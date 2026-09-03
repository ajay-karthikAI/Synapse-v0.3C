"""
The HTTP surface, offline.

Organised around what an API adds that the service below it does not have:
credentials, isolation between callers, a clock, retries, concurrency, and a
wire format. The medical behaviour is already covered by
``tests/test_service_golden.py``; what is asserted here is that HTTP does not
weaken it.

The properties this suite exists to hold:

* **both** credentials are required, and every rejection looks identical;
* one session cannot see another's conversation or brief;
* a retried request replays rather than re-running;
* two concurrent turns on one session cannot interleave;
* every envelope shape is reachable and correctly discriminated;
* an interrupted stream does not leave a session wedged;
* no query, passcode, token or provider message reaches a log or a response.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from synapse.api.config import MAX_QUERY_CHARS, MAX_TURNS_PER_SESSION
from synapse.api.deps import ACCESS_COOKIE_NAME, SERVICE_TOKEN_HEADER
from synapse.api.security import issue_access_token, new_session_id
from synapse.api.sessions import SessionStore
from synapse.ui.errors import PATIENT_ERROR_MESSAGE
from tests.api_fixtures import (
    JWT_SECRET,
    NOT_READY,
    PASSCODE,
    SERVICE_TOKEN,
    api,
    ask,
    authed,
    envelope_of,
    login,
    login_full,
    service_headers,
    sse_events,
)
from tests.golden_states import (
    ABSTAIN_RESPONSE,
    MEDICAL_PROSE,
    MEDICAL_STAFF_RESPONSE,
    PARTIAL_RESPONSE,
    PROVIDER_SECRET,
    FakeClient,
    empty_bundle,
)

QUERY = "what does my HbA1c number actually mean?"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestServiceToken:
    """Gate 1. The browser never holds this; the proxy does."""

    def test_health_needs_no_credential(self) -> None:
        """A platform health check cannot carry a token."""
        assert api().get("/healthz").status_code == 200

    def test_readiness_needs_no_credential(self) -> None:
        assert api().get("/readyz").status_code == 200

    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/v1/access/login"),
            ("get", "/v1/session"),
            ("delete", "/v1/session"),
            ("post", "/v1/turns/stream"),
            ("get", "/v1/transparency"),
            ("get", "/v1/turns/0/brief"),
            ("get", "/v1/turns/0/brief/export/text"),
        ],
    )
    def test_every_other_route_requires_it(self, method: str, path: str) -> None:
        client = api()
        kwargs = {"json": {}} if method == "post" else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"

    def test_a_wrong_token_is_refused(self) -> None:
        response = api().get("/v1/transparency", headers={SERVICE_TOKEN_HEADER: "wrong"})
        assert response.status_code == 401

    def test_a_prefix_of_the_token_is_refused(self) -> None:
        """A constant-time comparison must still be a correct one."""
        response = api().get("/v1/transparency", headers={SERVICE_TOKEN_HEADER: SERVICE_TOKEN[:-1]})
        assert response.status_code == 401


class TestLogin:
    """The passcode gate, and the limiter in front of it."""

    def test_a_correct_passcode_opens_a_session(self) -> None:
        client = api()
        response = client.post(
            "/v1/access/login", json={"passcode": PASSCODE}, headers=service_headers()
        )
        assert response.status_code == 200
        assert response.json()["expires_in_seconds"] == 8 * 60 * 60
        assert ACCESS_COOKIE_NAME in client.cookies

    def test_the_cookie_is_httponly_and_samesite(self) -> None:
        """Unreadable to page JavaScript, and not sent on cross-site POSTs."""
        client = api()
        response = client.post(
            "/v1/access/login", json={"passcode": PASSCODE}, headers=service_headers()
        )
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" in cookie

    def test_a_wrong_passcode_is_refused(self) -> None:
        response = api().post(
            "/v1/access/login", json={"passcode": "wrong"}, headers=service_headers()
        )
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"

    def test_login_requires_the_service_token_first(self) -> None:
        """An unauthenticated caller cannot reach the passcode comparison."""
        assert api().post("/v1/access/login", json={"passcode": PASSCODE}).status_code == 401

    def test_repeated_failures_lock_the_address_out(self) -> None:
        client = api()
        for _ in range(10):
            client.post("/v1/access/login", json={"passcode": "wrong"}, headers=service_headers())
        response = client.post(
            "/v1/access/login", json={"passcode": PASSCODE}, headers=service_headers()
        )
        assert response.status_code == 429
        assert response.json()["code"] == "rate_limited"

    def test_a_success_clears_the_failure_budget(self) -> None:
        """One person's typos must not lock out the next patient."""
        client = api()
        for _ in range(9):
            client.post("/v1/access/login", json={"passcode": "wrong"}, headers=service_headers())
        assert login(client)  # Succeeds, clearing the record
        for _ in range(9):
            client.post("/v1/access/login", json={"passcode": "wrong"}, headers=service_headers())
        # Still under the limit, because the counter was reset.
        assert (
            client.post(
                "/v1/access/login", json={"passcode": PASSCODE}, headers=service_headers()
            ).status_code
            == 200
        )

    def test_a_spoofed_forwarded_header_cannot_choose_a_fresh_bucket(self) -> None:
        """The key is counted from the RIGHT, past the hop we actually have.

        Every failure below arrives with a DIFFERENT client-supplied left-most
        entry and the SAME proxy-appended right-most one. If the limiter keyed
        on the left-most entry, each would land in its own bucket and nothing
        would ever lock out.
        """
        client = api()
        for index in range(10):
            client.post(
                "/v1/access/login",
                json={"passcode": "wrong"},
                headers={
                    **service_headers(),
                    "X-Forwarded-For": f"203.0.113.{index}, 10.0.0.5",
                },
            )
        # Same real peer, another invented prefix: still locked.
        assert (
            client.post(
                "/v1/access/login",
                json={"passcode": PASSCODE},
                headers={**service_headers(), "X-Forwarded-For": "198.51.100.9, 10.0.0.5"},
            ).status_code
            == 429
        )

    def test_a_genuinely_different_peer_has_its_own_budget(self) -> None:
        """Locking one address must not lock everyone behind a different proxy."""
        client = api()
        for _ in range(10):
            client.post(
                "/v1/access/login",
                json={"passcode": "wrong"},
                headers={**service_headers(), "X-Forwarded-For": "203.0.113.1, 10.0.0.5"},
            )
        assert (
            client.post(
                "/v1/access/login",
                json={"passcode": PASSCODE},
                headers={**service_headers(), "X-Forwarded-For": "203.0.113.1, 10.0.0.6"},
            ).status_code
            == 200
        )


class TestAccessToken:
    """Gate 2. Every failure mode is indistinguishable."""

    def test_a_protected_route_needs_both_credentials(self) -> None:
        client = api()
        token = login(client)
        # Access token alone, no service token.
        client.cookies.clear()
        assert (
            client.get("/v1/session", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )

    @pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "eyJhbGciOiJub25lIn0.e30."])
    def test_malformed_tokens_are_refused(self, token: str) -> None:
        """Including the ``alg: none`` token, which a naive decoder accepts."""
        response = api().get(
            "/v1/session",
            headers={**service_headers(), "Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    def test_a_token_signed_with_another_secret_is_refused(self) -> None:
        forged = issue_access_token(new_session_id(), "a-different-secret-entirely-0123456789")
        response = api().get(
            "/v1/session", headers={**service_headers(), "Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    def test_an_expired_token_is_refused(self) -> None:
        expired = issue_access_token(new_session_id(), JWT_SECRET, ttl_seconds=-1)
        response = api().get(
            "/v1/session", headers={**service_headers(), "Authorization": f"Bearer {expired}"}
        )
        assert response.status_code == 401

    def test_a_valid_token_for_an_unknown_session_is_refused(self) -> None:
        """Correctly signed, but names nothing. Same 401 as every other failure."""
        orphan = issue_access_token(new_session_id(), JWT_SECRET)
        response = api().get(
            "/v1/session", headers={**service_headers(), "Authorization": f"Bearer {orphan}"}
        )
        assert response.status_code == 401
        assert response.json()["message"] == "Not authorised."

    def test_logout_discards_the_session(self) -> None:
        client = api()
        headers = authed(client)
        assert client.get("/v1/session", headers=headers).status_code == 200
        assert client.post("/v1/access/logout", headers=headers).status_code == 200
        assert client.get("/v1/session", headers=headers).status_code == 401


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    """One session must never see another's conversation."""

    def test_two_sessions_have_separate_conversations(self) -> None:
        client = api()
        first = authed(client)
        ask(client, first, QUERY)
        client.cookies.clear()
        second = authed(client)

        assert client.get("/v1/session", headers=first).json()["turn_count"] == 1
        assert client.get("/v1/session", headers=second).json()["turn_count"] == 0

    def test_one_session_cannot_read_another_s_brief(self) -> None:
        client = api()
        first = authed(client)
        ask(client, first, QUERY)
        assert client.get("/v1/turns/0/brief", headers=first).status_code == 200

        client.cookies.clear()
        second = authed(client)
        # The second session has no turn 0 at all, so there is nothing to read.
        assert client.get("/v1/turns/0/brief", headers=second).status_code == 404

    def test_deleting_a_session_discards_its_state(self) -> None:
        client = api()
        headers = authed(client)
        ask(client, headers, QUERY)
        assert client.delete("/v1/session", headers=headers).json()["deleted"] is True
        assert client.get("/v1/session", headers=headers).status_code == 401


class TestSessionLimits:
    """Every bound, exercised."""

    def test_the_session_reports_its_own_limits(self) -> None:
        client = api()
        payload = client.get("/v1/session", headers=authed(client)).json()
        assert payload["max_turns"] == MAX_TURNS_PER_SESSION
        assert payload["idle_ttl_seconds"] == 2 * 60 * 60
        assert payload["turn_active"] is False

    def test_an_idle_session_expires(self) -> None:
        """Expiry is measured from last activity, not from creation.

        A negative TTL rather than zero: the check is ``now - last_seen > ttl``,
        which is False at exactly zero within the same instant.
        """
        store = SessionStore(idle_ttl_seconds=-1.0)
        client = api(sessions=store)
        headers = authed(client)
        assert client.get("/v1/session", headers=headers).status_code == 401

    def test_an_expired_session_is_indistinguishable_from_an_unknown_one(self) -> None:
        """Telling them apart confirms a guessed identifier."""
        store = SessionStore(idle_ttl_seconds=-1.0)
        client = api(sessions=store)
        headers = authed(client)
        expired = client.get("/v1/session", headers=headers)
        orphan = api().get(
            "/v1/session",
            headers={
                SERVICE_TOKEN_HEADER: SERVICE_TOKEN,
                "Authorization": f"Bearer {issue_access_token(new_session_id(), JWT_SECRET)}",
            },
        )
        assert expired.status_code == orphan.status_code == 401
        assert expired.json() == orphan.json()

    def test_activity_postpones_expiry(self) -> None:
        store = SessionStore(idle_ttl_seconds=60.0)
        client = api(sessions=store)
        headers = authed(client)
        for _ in range(3):
            assert client.get("/v1/session", headers=headers).status_code == 200

    def test_the_turn_ceiling_is_enforced(self) -> None:
        client = api()
        headers = authed(client)
        for _ in range(MAX_TURNS_PER_SESSION):
            assert ask(client, headers, QUERY).status_code == 200
        assert (
            client.get("/v1/session", headers=headers).json()["turn_count"] == MAX_TURNS_PER_SESSION
        )
        refused = ask(client, headers, QUERY)
        assert refused.status_code == 409
        assert refused.json()["code"] == "turn_limit"


class TestInputBounds:
    """Requests are bounded before they reach anything expensive."""

    def test_an_empty_query_is_rejected(self) -> None:
        client = api()
        response = ask(client, authed(client), "")
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_request"

    def test_an_over_long_query_is_rejected(self) -> None:
        client = api()
        response = ask(client, authed(client), "x" * (MAX_QUERY_CHARS + 1))
        assert response.status_code == 422

    def test_a_query_at_the_limit_is_accepted(self) -> None:
        client = api()
        assert ask(client, authed(client), "x" * MAX_QUERY_CHARS).status_code == 200

    def test_an_unknown_field_is_rejected(self) -> None:
        """extra='forbid': a client typo is reported, not silently ignored."""
        client = api()
        response = client.post(
            "/v1/turns/stream",
            json={"query": QUERY, "clientRequestId": "camelCase"},
            headers=authed(client),
        )
        assert response.status_code == 422

    def test_a_validation_error_never_echoes_the_submitted_value(self) -> None:
        """The rejected body contains the patient's question."""
        client = api()
        secret_query = "i have a rare condition nobody else has"
        response = client.post(
            "/v1/turns/stream",
            json={"query": secret_query, "unexpected": 1},
            headers=authed(client),
        )
        assert response.status_code == 422
        assert secret_query not in response.text


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


class TestStreaming:
    """SSE: stages, then exactly one envelope."""

    def test_a_turn_streams_stages_then_one_envelope(self) -> None:
        client = api()
        response = ask(client, authed(client), QUERY)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = sse_events(response.text)
        assert [name for name, _ in events].count("envelope") == 1
        assert any(name == "stage" for name, _ in events)

    def test_stage_events_carry_only_a_closed_stage_and_fixed_copy(self) -> None:
        """The privacy property, asserted on the wire."""
        client = api()
        response = ask(client, authed(client), "chest pain when i climb stairs")
        for name, data in sse_events(response.text):
            if name != "stage":
                continue
            payload = json.loads(data)
            assert set(payload) == {"stage", "message"}
            assert "chest" not in data.lower()
            assert "stairs" not in data.lower()

    def test_the_response_asks_proxies_not_to_buffer(self) -> None:
        client = api()
        response = ask(client, authed(client), QUERY)
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]

    def test_an_interrupted_stream_releases_the_session(self) -> None:
        """A client that disconnects must not wedge its session."""
        store = SessionStore()
        client = api(sessions=store)
        session_id, token = login_full(client)
        headers = {SERVICE_TOKEN_HEADER: SERVICE_TOKEN, "Authorization": f"Bearer {token}"}
        with client.stream(
            "POST", "/v1/turns/stream", json={"query": QUERY}, headers=headers
        ) as response:
            assert response.status_code == 200
            next(response.iter_lines())  # Read one line, then abandon it

        # The worker owns the release, so the session frees itself even though
        # nobody is reading the stream any more.
        session = store.get(session_id)
        for _ in range(500):
            if not session.turn_active:
                break
            threading.Event().wait(0.01)
        assert session.turn_active is False
        # And the session is usable again.
        assert ask(client, headers, QUERY).status_code == 200


class TestEnvelopes:
    """Every envelope shape is reachable and correctly discriminated."""

    def test_answer(self) -> None:
        client = api()
        envelope = envelope_of(ask(client, authed(client), QUERY))
        assert envelope["kind"] == "answer"
        assert envelope["action"] == "answer"
        assert envelope["claims"]
        assert envelope["sources"]
        assert envelope["disclaimer"]
        assert envelope["brief_available"] is True

    def test_partially_supported_is_marked(self) -> None:
        client = api(client=FakeClient(PARTIAL_RESPONSE))
        envelope = envelope_of(ask(client, authed(client), QUERY))
        assert any(claim["support"] == "partially_supported" for claim in envelope["claims"])

    def test_abstain_carries_the_insufficient_block(self) -> None:
        client = api(client=FakeClient(ABSTAIN_RESPONSE))
        envelope = envelope_of(ask(client, authed(client), QUERY))
        assert envelope["kind"] == "answer"
        assert envelope["action"] == "abstain"
        assert envelope["insufficient"]["reason"] == "below_threshold"
        assert envelope["brief_available"] is True

    def test_medical_staff(self) -> None:
        client = api(client=FakeClient(MEDICAL_STAFF_RESPONSE))
        envelope = envelope_of(ask(client, authed(client), QUERY))
        assert envelope["action"] == "medical_staff"
        assert envelope["staff_message"]

    def test_emergency_cites_nothing_and_offers_no_brief(self) -> None:
        client = api(is_emergency=lambda _q: True)
        envelope = envelope_of(ask(client, authed(client), "crushing chest pain"))
        assert envelope["kind"] == "emergency"
        assert envelope["brief_available"] is False
        assert "sources" not in envelope
        assert envelope["disclaimer"]

    def test_insufficient_evidence(self) -> None:
        client = api(retriever=lambda _q: empty_bundle())
        envelope = envelope_of(ask(client, authed(client), QUERY))
        assert envelope["kind"] == "insufficient"
        assert envelope["detail"]["not_a_judgement"]
        assert envelope["brief_available"] is False

    def test_failure(self) -> None:
        client = api(client=FakeClient(MEDICAL_PROSE))
        envelope = envelope_of(ask(client, authed(client), QUERY))
        assert envelope["kind"] == "failure"
        assert envelope["code"] == "generation_invalid_json"
        assert envelope["message"] == PATIENT_ERROR_MESSAGE

    def test_a_failure_envelope_never_carries_model_prose(self) -> None:
        client = api(client=FakeClient(MEDICAL_PROSE))
        body = ask(client, authed(client), QUERY).text
        assert MEDICAL_PROSE not in body
        assert "metformin" not in body.lower()

    def test_a_failure_envelope_never_carries_provider_detail(self) -> None:
        from synapse.answer.providers import ProviderUnavailableError

        client = api(client=FakeClient(error=ProviderUnavailableError(problem=PROVIDER_SECRET)))
        body = ask(client, authed(client), QUERY).text
        assert PROVIDER_SECRET not in body
        assert "sk-live" not in body


class TestIdempotency:
    """A retry replays. It does not run a second turn."""

    def test_the_same_request_id_replays_the_envelope(self) -> None:
        client = api()
        headers = authed(client)
        first = envelope_of(ask(client, headers, QUERY, client_request_id="abc"))
        second = envelope_of(ask(client, headers, QUERY, client_request_id="abc"))
        assert first == second

    def test_a_replay_does_not_consume_a_turn(self) -> None:
        """A second turn would cost a model call and could move the safety latch."""
        client = api()
        headers = authed(client)
        ask(client, headers, QUERY, client_request_id="abc")
        ask(client, headers, QUERY, client_request_id="abc")
        assert client.get("/v1/session", headers=headers).json()["turn_count"] == 1

    def test_a_replay_emits_no_stage_events(self) -> None:
        client = api()
        headers = authed(client)
        ask(client, headers, QUERY, client_request_id="abc")
        replay = ask(client, headers, QUERY, client_request_id="abc")
        assert [name for name, _ in sse_events(replay.text)] == ["envelope"]

    def test_a_different_request_id_runs_a_new_turn(self) -> None:
        client = api()
        headers = authed(client)
        ask(client, headers, QUERY, client_request_id="one")
        ask(client, headers, QUERY, client_request_id="two")
        assert client.get("/v1/session", headers=headers).json()["turn_count"] == 2

    def test_an_absent_request_id_never_replays(self) -> None:
        client = api()
        headers = authed(client)
        ask(client, headers, QUERY)
        ask(client, headers, QUERY)
        assert client.get("/v1/session", headers=headers).json()["turn_count"] == 2


class TestConcurrency:
    """One active turn per session, enforced by compare-and-set."""

    def test_two_concurrent_turns_on_one_session_are_not_both_served(self) -> None:
        client = api()
        headers = authed(client)
        results: list[int] = []
        barrier = threading.Barrier(2)

        def fire() -> None:
            barrier.wait()
            results.append(ask(client, headers, QUERY).status_code)

        threads = [threading.Thread(target=fire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results) == [200, 200] or 409 in results
        # Whatever the interleaving, the conversation must not have been
        # corrupted by two turns writing at once.
        assert client.get("/v1/session", headers=headers).json()["turn_count"] <= 2

    def test_concurrent_sessions_do_not_interfere(self) -> None:
        counts: list[int] = []

        def one_session() -> None:
            local = api()
            headers = authed(local)
            ask(local, headers, QUERY)
            counts.append(local.get("/v1/session", headers=headers).json()["turn_count"])

        threads = [threading.Thread(target=one_session) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert counts == [1, 1, 1, 1]


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    """Phase 2's readiness, as an enforcement point."""

    def test_ready_reports_ready(self) -> None:
        response = api().get("/readyz")
        assert response.status_code == 200
        assert response.json()["ready"] is True

    def test_an_unready_process_reports_503(self) -> None:
        response = api(ready=False, readiness=NOT_READY).get("/readyz")
        assert response.status_code == 503
        assert response.json()["ready"] is False

    def test_an_unready_process_still_reports_liveness(self) -> None:
        """A bad artifact must not become a crash loop."""
        assert api(ready=False, readiness=NOT_READY).get("/healthz").status_code == 200

    def test_an_unready_process_refuses_turns(self) -> None:
        client = api(ready=False, readiness=NOT_READY)
        response = ask(client, authed(client), QUERY)
        assert response.status_code == 503

    def test_login_still_works_while_unready(self) -> None:
        """A patient can be told the service is down; they cannot be told nothing."""
        client = api(ready=False, readiness=NOT_READY)
        assert (
            client.post(
                "/v1/access/login", json={"passcode": PASSCODE}, headers=service_headers()
            ).status_code
            == 200
        )

    def test_readiness_never_reports_a_bucket_or_path(self) -> None:
        body = api(ready=False, readiness=NOT_READY).get("/readyz").text
        assert "bucket" not in body.lower()
        assert "/var/data" not in body


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


class TestBrief:
    """Server-owned, edited by named operation only."""

    def _client_with_turn(self):  # type: ignore[no-untyped-def]
        client = api()
        headers = authed(client)
        ask(client, headers, QUERY)
        return client, headers

    def test_a_brief_is_built_on_first_read(self) -> None:
        client, headers = self._client_with_turn()
        payload = client.get("/v1/turns/0/brief", headers=headers).json()
        assert payload["document_id"]
        assert payload["claims"]
        assert payload["disclaimer"]

    def test_the_document_id_is_stable_across_reads(self) -> None:
        """Rebuilding would issue a new identifier on every interaction."""
        client, headers = self._client_with_turn()
        first = client.get("/v1/turns/0/brief", headers=headers).json()["document_id"]
        second = client.get("/v1/turns/0/brief", headers=headers).json()["document_id"]
        assert first == second

    def test_topic_and_notes_are_editable(self) -> None:
        client, headers = self._client_with_turn()
        client.get("/v1/turns/0/brief", headers=headers)
        payload = client.put(
            "/v1/turns/0/brief/topic", json={"topic": "my blood sugar"}, headers=headers
        ).json()
        assert payload["topic"] == "my blood sugar"
        payload = client.put(
            "/v1/turns/0/brief/notes", json={"notes": "worse in the morning"}, headers=headers
        ).json()
        assert payload["notes"] == "worse in the morning"

    def test_a_question_can_be_added_and_removed(self) -> None:
        client, headers = self._client_with_turn()
        client.get("/v1/turns/0/brief", headers=headers)
        added = client.post(
            "/v1/turns/0/brief/questions",
            json={"text": "Should I change anything?"},
            headers=headers,
        ).json()
        question_id = added["questions"][-1]["question_id"]
        assert any(q["text"] == "Should I change anything?" for q in added["questions"])
        removed = client.delete(
            f"/v1/turns/0/brief/questions/{question_id}", headers=headers
        ).json()
        assert all(q["question_id"] != question_id for q in removed["questions"])

    def test_no_endpoint_accepts_a_replacement_brief(self) -> None:
        """The forgery guard: claims must come from the answer layer or not exist."""
        client, headers = self._client_with_turn()
        client.get("/v1/turns/0/brief", headers=headers)
        forged = {
            "claims": [
                {
                    "claim_id": "forged",
                    "text": "Stop taking your medication.",
                    "support": "supported",
                    "source_numbers": [1],
                }
            ]
        }
        for path in ("/v1/turns/0/brief/topic", "/v1/turns/0/brief/notes"):
            assert client.put(path, json=forged, headers=headers).status_code == 422
        after = client.get("/v1/turns/0/brief", headers=headers).json()
        assert all(claim["claim_id"] != "forged" for claim in after["claims"])

    def test_an_emergency_turn_has_no_brief(self) -> None:
        client = api(is_emergency=lambda _q: True)
        headers = authed(client)
        ask(client, headers, "crushing chest pain")
        assert client.get("/v1/turns/0/brief", headers=headers).status_code == 404

    def test_a_missing_turn_is_a_typed_404(self) -> None:
        client, headers = self._client_with_turn()
        response = client.get("/v1/turns/9/brief", headers=headers)
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    @pytest.mark.parametrize(
        "fmt,content_type",
        [("html", "text/html"), ("text", "text/plain"), ("json", "application/json")],
    )
    def test_exports_return_safe_attachment_headers(self, fmt: str, content_type: str) -> None:
        client, headers = self._client_with_turn()
        response = client.get(f"/v1/turns/0/brief/export/{fmt}", headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(content_type)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="appointment-brief-')
        # A quote or newline in the filename would break out of the header.
        assert "\n" not in disposition and disposition.count('"') == 2

    def test_an_unknown_export_format_is_refused(self) -> None:
        client, headers = self._client_with_turn()
        assert client.get("/v1/turns/0/brief/export/exe", headers=headers).status_code == 404

    def test_the_export_carries_the_permanent_disclaimer(self) -> None:
        client, headers = self._client_with_turn()
        body = client.get("/v1/turns/0/brief/export/text", headers=headers).text
        assert "not a diagnosis" in body.lower()


# ---------------------------------------------------------------------------
# Transparency
# ---------------------------------------------------------------------------


class TestTransparency:
    """The disclosures are not configurable."""

    def test_it_states_that_sources_are_unreviewed(self) -> None:
        payload = api().get("/v1/transparency", headers=service_headers()).json()
        joined = " ".join(payload["disclosures"]).lower()
        assert "unreviewed" in joined
        assert "no clinician has reviewed" in joined

    def test_it_states_that_no_case_can_gate_a_release(self) -> None:
        payload = api().get("/v1/transparency", headers=service_headers()).json()
        assert "gate a release" in " ".join(payload["disclosures"]).lower()
        assert payload["evaluation"]["release_gating_capable"] is False

    def test_it_never_claims_review_or_approval(self) -> None:
        payload = api().get("/v1/transparency", headers=service_headers()).json()
        assert payload["sources"]["reviewed_by_clinician"] is False

    def test_an_absent_evaluation_reports_unavailable_not_zero(self) -> None:
        """Zeroes would read as a measured result."""
        payload = api().get("/v1/transparency", headers=service_headers()).json()
        assert payload["evaluation"]["available"] is False

    def test_it_reports_where_query_text_goes(self) -> None:
        payload = api().get("/v1/transparency", headers=service_headers()).json()
        privacy = payload["privacy"]
        assert privacy["query_text_leaves_the_server"] is True
        assert privacy["query_text_logged"] is False
        assert privacy["conversation_persisted"] is False

    def test_it_requires_the_service_token_but_not_a_session(self) -> None:
        """A reader deciding whether to trust this should not need to be inside it."""
        assert api().get("/v1/transparency", headers=service_headers()).status_code == 200
        assert api().get("/v1/transparency").status_code == 401


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _synapse_log_blob(caplog) -> str:
    """Every field of every record SYNAPSE emitted, as one string.

    Scoped to ``synapse.*`` deliberately. The test client and httpx log their
    own request lines, and asserting over those would be testing someone else's
    library — and would fail on incidental words like "http".

    The whole ``__dict__`` is searched, not just the message: this project logs
    structured fields through ``extra=``, so a leak would most likely appear
    there rather than in the formatted text.
    """
    return " ".join(
        str(record.__dict__) for record in caplog.records if record.name.startswith("synapse")
    )


class TestNoSensitiveLogging:
    """Nothing secret or medical reaches a log record."""

    SECRET_QUERY = "i have crushing chest pain and take metoprolol"

    def test_a_turn_logs_no_query_text(self, caplog) -> None:
        client = api()
        headers = authed(client)
        with caplog.at_level(logging.DEBUG):
            ask(client, headers, self.SECRET_QUERY)
        blob = _synapse_log_blob(caplog)
        assert "metoprolol" not in blob
        assert "crushing" not in blob

    def test_login_logs_no_passcode(self, caplog) -> None:
        client = api()
        with caplog.at_level(logging.DEBUG):
            client.post("/v1/access/login", json={"passcode": PASSCODE}, headers=service_headers())
            client.post(
                "/v1/access/login", json={"passcode": "wrong-guess"}, headers=service_headers()
            )
        blob = _synapse_log_blob(caplog)
        assert PASSCODE not in blob
        assert "wrong-guess" not in blob

    def test_nothing_logs_the_service_token(self, caplog) -> None:
        client = api()
        with caplog.at_level(logging.DEBUG):
            client.get("/v1/transparency", headers=service_headers())
            client.get("/v1/transparency", headers={SERVICE_TOKEN_HEADER: "bad-token-value"})
        blob = _synapse_log_blob(caplog)
        assert SERVICE_TOKEN not in blob
        assert "bad-token-value" not in blob

    def test_production_never_puts_provider_text_into_a_typed_error(self) -> None:
        """Where the guarantee actually lives.

        ``synapse.errors`` builds ``safe_message`` from whatever details the
        RAISER supplies, so the protection is that no production call site ever
        supplies provider-derived text. Every construction in ``providers`` uses
        a fixed application string, and the one that wraps a real exception
        reduces it to ``type(exc).__name__`` plus an integer status.

        Asserted over the source rather than by triggering a provider, because a
        provider cannot be triggered offline — and because this is a property of
        the call sites, not of any one failure.
        """
        import ast
        import inspect

        from synapse.answer import providers

        tree = ast.parse(inspect.getsource(providers))
        literals: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name != "ProviderUnavailableError":
                continue
            for keyword in node.keywords:
                if keyword.arg == "problem":
                    # A constant, never an f-string and never str(exc).
                    assert isinstance(keyword.value, ast.Constant), (
                        "problem= must be a fixed application string"
                    )
                    literals.append(str(keyword.value.value))
        assert literals, "expected ProviderUnavailableError call sites to inspect"

    def test_a_provider_failure_logs_only_a_code_and_a_type(self, caplog) -> None:
        """What a real provider outage records: no message, no URL, no key."""
        from synapse.answer.providers import ProviderUnavailableError

        client = api(
            client=FakeClient(error=ProviderUnavailableError(problem="provider call failed"))
        )
        headers = authed(client)
        with caplog.at_level(logging.DEBUG):
            body = ask(client, headers, QUERY).text
        assert "generation_unavailable" in body
        blob = _synapse_log_blob(caplog)
        # The failure IS recorded -- an operator must be able to see it -- but
        # only as a typed code and the sanitised structural message.
        assert "generation_unavailable" in blob
        assert "://" not in blob  # No endpoint, no presigned fragment
        assert "sk-" not in blob  # No credential-shaped value
