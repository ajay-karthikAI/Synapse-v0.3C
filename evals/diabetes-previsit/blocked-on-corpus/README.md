# Blocked on an approved corpus

Ten drafted cases that **cannot yet be added to the dataset**. Each expects
`answer`, and the dataset rule is:

    a case expecting 'answer' must grade at least one document 2 or higher

That rule is correct. An `answer` expectation with no graded document cannot be
scored for retrieval, so the case would silently contribute nothing to
`recall_at_k`, `ndcg_at_k` or `mrr` while appearing to be a real case.

Relevance grades are pinned to a corpus version, and this dataset's
`corpus_version` is `UNSET-no-approved-corpus-exists`. Grading these against the
current 847-document legacy corpus would be meaningless: none of it is approved,
and the labels would be invalid the moment a governed corpus replaced it.

**So the ordering is forced.** Approve a corpus first, then grade these, then add
them. It cannot be done the other way round, and no amount of case authoring
substitutes for it.

## To complete a case once a corpus exists

1. Set the dataset's `corpus_version` to the approved corpus.
2. Add `relevant_documents` entries with `grade >= 2` for the documents that
   should be retrieved.
3. `python -m synapse.cli.evalset add-case --dataset evals/diabetes-previsit --case <file>`

The expected behaviour, required concepts and forbidden claims in each file are
engineering-authored **proposals**, exactly like the cases already in the
dataset. They carry no clinical authority and are for a reviewer to accept or
reject.
