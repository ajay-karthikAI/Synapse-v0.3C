"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { TurnView } from "@/components/answer/TurnView";
import { BriefPanel } from "@/components/brief/BriefPanel";
import { Composer } from "@/components/chat/Composer";
import { StageTimeline } from "@/components/chat/StageTimeline";
import { offersBrief, type TurnEnvelope } from "@/lib/envelope";
import { deleteSession, readSession } from "@/lib/session-client";
import { askTurn, newRequestId, type StageEvent, type TransportFailure } from "@/lib/turns";

/**
 * The conversation: composer, progress, and every completed turn.
 *
 * Five behaviours here are worth stating outright, because each of them is a
 * decision rather than a default.
 *
 * **It keeps going.** Answering does not end anything: the session stays open,
 * turns accumulate on the page, and the composer moves below the newest answer
 * so the next question is asked from the bottom of the conversation rather than
 * from a box above it. There is still exactly ONE composer in the document —
 * the field carries a fixed `id`, and two of them would leave a screen reader
 * choosing between two identically labelled boxes. Follow-ups are resolved
 * against the session's history on the server
 * (`synapse.memory.query_rewrite`), which is why a bare "what about the side
 * effects?" is a complete question here.
 *
 * **One turn at a time.** `pending` gates the composer and the submit handler.
 * The server enforces the same rule and answers a second concurrent turn with
 * 409, but by then the request has already cost a slot against the session's
 * question ceiling, so the client refuses first.
 *
 * **The draft is never destroyed by a failure.** `draft` is cleared only after
 * an envelope has actually arrived. Every transport failure — expired session,
 * dropped connection, backend restart — leaves the text in the box, and the
 * retry re-sends it under the *same* `client_request_id`, which the server
 * treats as idempotent: a turn that already ran is replayed rather than run
 * twice.
 *
 * **A conversation is not restorable, and the interface says so.** The API
 * exposes counts and clocks but never conversation content — deliberately, so
 * that a stolen token cannot be turned into a transcript
 * (`synapse/api/routes/session.py`). So on load this reads how many turns the
 * server still holds and tells the patient plainly that the earlier text cannot
 * be shown, rather than pretending the session is empty or inventing a history.
 *
 * **Clearing clears both halves.** The server forgets the session and the page
 * drops its rendered turns. Either alone leaves the other holding the
 * conversation.
 */

interface CompletedTurn {
  key: string;
  question: string;
  envelope: TurnEnvelope;
  /** The id this turn was sent under, so a retry is idempotent. */
  requestId: string;
}

interface ConversationProps {
  examples: readonly string[];
}

/** Copy for each transport failure. Application-owned; no server string is shown. */
const TRANSPORT_COPY: Record<TransportFailure, string> = {
  network:
    "The connection dropped before an answer arrived. Your question is still in the box — try again.",
  unauthorized: "Your session has expired. Sign in again, and your question will still be here.",
  busy: "A question is already being answered. Wait for it to finish, then try again.",
  turn_limit:
    "This session has reached its limit of questions. Clear the conversation to start a new one.",
  unavailable:
    "The service is not available right now. Nothing was sent. Your question is still in the box.",
  malformed: "The answer could not be read. Nothing is being shown rather than something partial.",
};

export function Conversation({ examples }: ConversationProps) {
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<CompletedTurn[]>([]);
  const [pending, setPending] = useState(false);
  const [stages, setStages] = useState<StageEvent[]>([]);
  const [heartbeats, setHeartbeats] = useState(0);
  const [failure, setFailure] = useState<TransportFailure | null>(null);
  const [restored, setRestored] = useState<number | null>(null);
  const [openBrief, setOpenBrief] = useState<number | null>(null);

  const field = useRef<HTMLTextAreaElement>(null);
  // Held across a retry so the same question keeps its idempotency key.
  const requestId = useRef<string>(newRequestId());

  // Session restoration. See the note above: counts only, never content.
  useEffect(() => {
    let live = true;
    void readSession().then((session) => {
      if (live && session && session.turn_count > 0) setRestored(session.turn_count);
    });
    return () => {
      live = false;
    };
  }, []);

  const send = useCallback(
    async (question: string) => {
      setPending(true);
      setFailure(null);
      setStages([]);
      setHeartbeats(0);

      const result = await askTurn({
        query: question,
        clientRequestId: requestId.current,
        onStage: (stage) =>
          // Replace an existing stage rather than appending twice: a retried
          // turn can revisit one, and a duplicated line reads as a stall.
          setStages((current) =>
            current.some((item) => item.stage === stage.stage) ? current : [...current, stage],
          ),
        onHeartbeat: () => setHeartbeats((count) => count + 1),
      });

      setPending(false);
      setStages([]);

      if (!result.ok) {
        // The draft is untouched, and `requestId` is unchanged, so pressing Ask
        // again replays rather than runs a second turn.
        setFailure(result.failure);
        return;
      }

      setTurns((current) => [
        ...current,
        {
          key: `turn-${current.length}-${requestId.current.slice(0, 8)}`,
          question,
          envelope: result.envelope,
          requestId: requestId.current,
        },
      ]);
      // Only now: an envelope actually arrived.
      setDraft("");
      requestId.current = newRequestId();
      setRestored(null);
    },
    [],
  );

  const clearConversation = useCallback(async () => {
    await deleteSession();
    setTurns([]);
    setFailure(null);
    setRestored(null);
    setOpenBrief(null);
    requestId.current = newRequestId();
    field.current?.focus();
  }, []);

  const started = turns.length > 0 || pending || failure !== null;
  // Once a turn has been answered the composer moves below it, so the page
  // reads in the order the conversation happened and the box to continue in is
  // the last thing on it. Gated on answered turns rather than on `started` on
  // purpose: moving it the instant Ask is pressed would unmount the button that
  // currently holds focus, and a screen-reader user would be dropped to the
  // document body while waiting. At this boundary the turn that just arrived
  // takes focus to its own heading, so the move costs nothing.
  const conversing = turns.length > 0;

  const composer = (
    <Composer
      value={draft}
      onChange={setDraft}
      onSubmit={() => void send(draft.trim())}
      busy={pending}
      examples={examples}
      showExamples={!started}
      followUp={conversing}
      fieldRef={field}
    />
  );

  const progress = pending ? <StageTimeline stages={stages} heartbeats={heartbeats} /> : null;

  const failureNotice = failure ? (
    <div
      role="status"
      className="mx-auto mt-8 max-w-xl rounded-[16px] border border-rule bg-surface px-5 py-4"
    >
      <p className="text-[15px] leading-relaxed text-ink">{TRANSPORT_COPY[failure]}</p>
      {failure === "unauthorized" ? (
        <a
          href="/access"
          className="mt-3 inline-flex min-h-[44px] items-center text-[14px] text-accent underline-offset-4 hover:underline"
        >
          Sign in again
        </a>
      ) : (
        <button
          type="button"
          onClick={() => void send(draft.trim())}
          disabled={draft.trim().length === 0}
          className="mt-3 inline-flex min-h-[44px] items-center rounded-[12px] bg-accent-royal px-5 text-[15px] font-medium text-white disabled:opacity-40"
        >
          Try again
        </button>
      )}
    </div>
  ) : null;

  return (
    <div className="mx-auto w-full max-w-3xl px-5 pb-24">
      {restored !== null && turns.length === 0 ? (
        <div
          role="status"
          className="mx-auto mb-8 max-w-xl rounded-[16px] border border-rule bg-surface px-5 py-4"
        >
          <p className="text-[14px] leading-relaxed text-ink">
            This session already has{" "}
            {restored === 1 ? "one question" : `${restored} questions`} on the
            server. Synapse never stores what was asked or answered, so earlier
            turns cannot be shown again — but the conversation is still open, so
            a follow-up will be understood in context.
          </p>
          <button
            type="button"
            onClick={() => void clearConversation()}
            className="mt-3 min-h-[44px] text-[14px] text-accent underline-offset-4 hover:underline"
          >
            Start a new conversation instead
          </button>
        </div>
      ) : null}

      {/* Before the first answer the composer is the page: centred, with the
          example chips under it. */}
      {conversing ? null : (
        <>
          {composer}
          {progress}
          {failureNotice}
        </>
      )}

      {turns.length > 0 ? (
        <div className="mt-14 space-y-14">
          {turns.map((turn, index) => (
            <TurnView
              key={turn.key}
              envelope={turn.envelope}
              question={turn.question}
              turnKey={turn.key}
              autoFocus={index === turns.length - 1}
              onRetry={() => {
                // Re-ask the same question. The stored id makes it a replay.
                requestId.current = turn.requestId;
                setDraft(turn.question);
                void send(turn.question);
              }}
              briefSlot={
                offersBrief(turn.envelope) ? (
                  <BriefSection
                    open={openBrief === index}
                    onOpen={() => setOpenBrief(index)}
                    onClose={() => setOpenBrief(null)}
                  />
                ) : null
              }
            />
          ))}
        </div>
      ) : null}

      {/* The conversation continues here. Progress and any transport failure
          sit above the box, next to the answer they belong to, and the box
          itself is the last thing before the clear control — so the reading
          order is question, answer, ask again. */}
      {conversing ? (
        <div className="mt-14">
          {progress}
          {failureNotice}
          <div className="mt-10">{composer}</div>
        </div>
      ) : null}

      {turns.length > 0 ? (
        <div className="mt-16 border-t border-rule pt-6 text-center">
          <button
            type="button"
            onClick={() => void clearConversation()}
            className="min-h-[44px] text-[14px] text-ink-secondary underline-offset-4 transition-colors hover:text-display hover:underline"
          >
            Clear this conversation
          </button>
          <p className="mt-2 text-[12px] leading-relaxed text-ink-secondary">
            Forgets everything on the server and clears this page. Nothing is
            archived, because there is nowhere to archive it to.
          </p>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The brief, and the control that reveals it.
 *
 * Rendered only where `brief_available` is true, which the server sets — so an
 * emergency, an insufficient-evidence result and a failure have no button here
 * at all, rather than a disabled one. There is nothing to build a brief from on
 * those paths, and offering a control that cannot work is worse than offering
 * none.
 *
 * Every one of these buttons opens the SAME document. The brief is one sheet
 * for the whole conversation, so the turn index only decides which disclosure
 * is expanded, not which brief is shown — a patient who asks four questions
 * gets one brief covering all four, not four partial ones.
 */
function BriefSection({
  open,
  onOpen,
  onClose,
}: {
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
}) {
  return (
    <div className="mt-8">
      {/*
        The trigger stays mounted while the panel is open, as a disclosure
        button whose `aria-expanded` reports the state. Unmounting it would
        destroy the element the panel restores focus to when it closes — focus
        would land on the document body, and the next Tab would start from the
        top of the page rather than from where the reader was.
      */}
      <button
        type="button"
        onClick={open ? onClose : onOpen}
        aria-expanded={open}
        className="inline-flex min-h-[44px] items-center gap-2 rounded-[12px] border border-accent-soft/40 bg-accent-wash px-5 text-[15px] font-medium text-accent transition-colors hover:border-accent"
      >
        {open ? "Hide the appointment brief" : "Build an appointment brief"}
        <span aria-hidden="true">{open ? "×" : "→"}</span>
      </button>
      <BriefPanel open={open} onClose={onClose} />
    </div>
  );
}
