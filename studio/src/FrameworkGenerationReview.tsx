/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import type { Connection } from "./api";
import { FrameworkCorpusSelector } from "./FrameworkCorpusSources";
import { FrameworkGenerationCandidateSelector } from "./FrameworkGenerationCandidateSelector";
import type { FrameworkGenerationCandidate } from "./FrameworkGenerationCandidates";
import {
  generationTargetBody,
  type FrameworkCorpus,
  type FrameworkGenerationBody,
  type FrameworkStatus,
} from "./FrameworkKnowledgeData";

function ExactReference({ value }: { value: string }) {
  return (
    <code>
      {value.match(/.{1,8}/g)?.map((part, index) => (
        <span className="framework-reference-part" key={index}>
          {part}
        </span>
      ))}
    </code>
  );
}

export function FrameworkGenerationReview(props: {
  connection: Connection;
  status: FrameworkStatus;
  busy: boolean;
  onActivate: (corpus: FrameworkCorpus, validationRunId: string) => void;
}) {
  return <GenerationReview key={props.status.framework} {...props} />;
}

function GenerationReview({
  connection,
  status,
  busy,
  onActivate,
}: {
  connection: Connection;
  status: FrameworkStatus;
  busy: boolean;
  onActivate: (corpus: FrameworkCorpus, validationRunId: string) => void;
}) {
  const [selected, setSelected] = useState<FrameworkCorpus | null>(null);
  const [candidate, setCandidate] =
    useState<FrameworkGenerationCandidate | null>(null);
  const [review, setReview] = useState<{
    corpus: FrameworkCorpus;
    body: FrameworkGenerationBody;
    candidate: FrameworkGenerationCandidate;
  } | null>(null);
  const [error, setError] = useState("");
  return (
    <details className="framework-generation-review">
      <summary>Review generation default</summary>
      <p>
        Choose an exact ready corpus and its completed native validation run.
        The server verifies runtime, implementation and QA receipts. Indexing
        alone does not activate a default.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError("");
          setReview(null);
          try {
            if (!selected)
              throw new Error("Choose a ready retained corpus first.");
            if (!candidate)
              throw new Error("Choose an eligible validation run first.");
            setReview({
              corpus: selected,
              candidate,
              body: generationTargetBody(
                status,
                selected,
                candidate.validation_run_id,
              ),
            });
          } catch (cause) {
            setError(
              cause instanceof Error
                ? cause.message
                : "The activation review is incomplete.",
            );
          }
        }}
      >
        <fieldset disabled={busy}>
          <legend>Native validation evidence</legend>
          <FrameworkCorpusSelector
            connection={connection}
            framework={status.framework}
            selected={selected}
            inputId={`generation-corpus-${status.framework}`}
            onSelect={(value) => {
              setSelected(value);
              setCandidate(null);
              setReview(null);
            }}
          />
          {selected?.state === "ready" ? (
            <FrameworkGenerationCandidateSelector
              key={`${selected.framework}:${selected.corpus_ref}`}
              connection={connection}
              corpus={selected}
              selected={candidate}
              onSelect={(value) => {
                setCandidate(value);
                setReview(null);
              }}
            />
          ) : (
            <p className="subtle">
              Choose a ready corpus to read its eligible validation runs.
            </p>
          )}
          <button
            className="secondary"
            disabled={!candidate || selected?.state !== "ready"}
          >
            Review exact target
          </button>
        </fieldset>
      </form>
      {error && <p role="alert">{error}</p>}
      {review && (
        <section
          className="framework-activation-confirm"
          aria-label="Generation activation review"
        >
          <h4>Confirm generation target</h4>
          <p>
            {review.corpus.framework} {review.corpus.framework_version} ·{" "}
            {review.corpus.release_tag}
          </p>
          <dl>
            <dt>Exact corpus</dt>
            <dd>
              <ExactReference value={review.corpus.corpus_ref} />
            </dd>
            <dt>Source commit</dt>
            <dd>
              <ExactReference value={review.corpus.commit} />
            </dd>
            <dt>Repository / component</dt>
            <dd>
              {review.candidate.repository_name} /{" "}
              {review.candidate.component_id}
            </dd>
            <dt>Validated runtime</dt>
            <dd>
              {review.candidate.runtime.runtime}{" "}
              {review.candidate.runtime.version}
            </dd>
            <dt>Native validation run</dt>
            <dd>
              <ExactReference value={review.body.validation_run_id} />
            </dd>
          </dl>
          <p>
            This changes the default for eligible new builds. Existing projects,
            dependencies and frozen runs keep their pins.
          </p>
          <button
            className="primary"
            disabled={
              busy || review.body.expected_status_revision !== status.revision
            }
            onClick={() =>
              onActivate(review.corpus, review.body.validation_run_id)
            }
          >
            Activate reviewed target
          </button>
        </section>
      )}
    </details>
  );
}
