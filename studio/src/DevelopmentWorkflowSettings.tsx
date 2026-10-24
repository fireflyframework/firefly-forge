/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { GitBranch, RefreshCw, Save } from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import {
  policyAcknowledgment,
  policyRequest,
  policyView,
  readPolicy,
  type GitDeliveryPolicy,
  type GitDeliveryPolicyView,
} from "./GitDeliveryData";
import "./GitDeliveries.css";

export function DevelopmentWorkflowSettings({
  connection,
  projectId,
}: {
  connection: Connection;
  projectId: string;
}) {
  const [current, setCurrent] = useState<GitDeliveryPolicyView | null>(null);
  const [draft, setDraft] = useState<GitDeliveryPolicy | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const live = useRef(false),
    reading = useRef<AbortController | null>(null),
    saving = useRef(false);
  const scope = useRef({ connection, projectId });
  scope.current = { connection, projectId };
  const path = `/projects/${projectId}/git-delivery-policy`;
  async function load() {
    const expected = scope.current;
    reading.current?.abort();
    const controller = new AbortController();
    reading.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = policyView(
        await request<unknown>(connection, path, { signal: controller.signal }),
        projectId,
      );
      if (
        !live.current ||
        controller.signal.aborted ||
        scope.current.connection !== expected.connection ||
        scope.current.projectId !== expected.projectId
      )
        return;
      setCurrent(value);
      setDraft(value.policy);
    } catch {
      if (
        live.current &&
        !controller.signal.aborted &&
        scope.current.connection === expected.connection &&
        scope.current.projectId === expected.projectId
      ) {
        setCurrent(null);
        setDraft(null);
        setError(
          "The current workflow could not be read and verified. Refresh it to check your access and saved settings.",
        );
      }
    } finally {
      if (live.current && reading.current === controller) {
        reading.current = null;
        setBusy(false);
      }
    }
  }
  useEffect(() => {
    live.current = true;
    void load();
    return () => {
      live.current = false;
      reading.current?.abort();
    };
    // The parent remounts this panel on project, connection and identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  let valid = false;
  try {
    if (draft) {
      readPolicy(draft);
      valid = true;
    }
  } catch {
    /* Invalid numeric drafts remain editable. */
  }
  const editable = !!current?.permissions.configure;
  const remoteAvailable =
    !!current?.permissions.publish &&
    !!current.remote_capabilities.length &&
    current.remote_capabilities.every((item) => item.draft_updates);
  const changed =
    !!current &&
    !!draft &&
    JSON.stringify(draft) !== JSON.stringify(current.policy);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!current || !draft || busy || saving.current || !valid || !changed)
      return;
    const expected = scope.current,
      previous = current,
      desired = { ...draft };
    const requestId = crypto.randomUUID().replaceAll("-", "");
    let body;
    try {
      body = policyRequest(previous, desired, requestId);
    } catch {
      setError(
        "Your current permissions or repository capabilities do not allow this workflow. Refresh the saved settings before continuing.",
      );
      return;
    }
    saving.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    let message = "",
      refused = false;
    try {
      const response = await request<unknown>(connection, path, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      policyAcknowledgment(response, previous, desired, requestId);
      message = desired.enabled
        ? "Workflow saved for new runs. Runs already in progress keep their recorded schedule and delivery settings."
        : "Checkpoints turned off for new and existing runs. Saved commits remain.";
    } catch (cause) {
      refused =
        cause instanceof ApiError &&
        cause.responseKind === "http" &&
        [401, 403, 409, 422].includes(cause.status);
      message = refused
        ? "The server declined this change. Review current access and settings before saving again. No automatic retry was sent."
        : "The save outcome was not confirmed. Read the current settings before deciding what to do next. No automatic retry was sent.";
    } finally {
      if (
        live.current &&
        scope.current.connection === expected.connection &&
        scope.current.projectId === expected.projectId
      ) {
        await load();
        if (
          live.current &&
          scope.current.connection === expected.connection &&
          scope.current.projectId === expected.projectId
        ) {
          if (refused) setError(message);
          else setNotice(message);
        }
      }
      saving.current = false;
    }
  }
  return (
    <section
      className="integration-card git-delivery-settings"
      aria-label="Development workflow"
    >
      <h3>
        <GitBranch size={20} aria-hidden="true" /> Development workflow
      </h3>
      <p>
        Save reviewable Git checkpoints as implementation work progresses. New
        runs use this schedule and delivery selection.
      </p>
      <form onSubmit={(event) => void save(event)}>
        <div className="git-delivery-activation">
          <label className="git-delivery-check">
            <input
              type="checkbox"
              aria-describedby="delivery-enabled-help"
              checked={draft?.enabled ?? false}
              disabled={busy || !editable || !draft}
              onChange={(event) =>
                draft && setDraft({ ...draft, enabled: event.target.checked })
              }
            />
            Save development checkpoints
          </label>
          <p id="delivery-enabled-help" className="subtle">
            Turning this off also stops further checkpoints for existing runs.
            Saved commits remain.
          </p>
        </div>
        <fieldset disabled={busy || !editable || !draft || !draft.enabled}>
          <legend>Checkpoint schedule and delivery</legend>
          <div className="git-delivery-fields">
            <div className="field">
              <label htmlFor="delivery-every-units">
                Completed implementation rounds between checkpoints
              </label>
              <input
                id="delivery-every-units"
                type="number"
                min={1}
                max={20}
                value={draft?.every_units ?? ""}
                onChange={(event) =>
                  draft &&
                  setDraft({
                    ...draft,
                    every_units: Number(event.target.value),
                  })
                }
              />
            </div>
            <div className="field">
              <label htmlFor="delivery-limit">
                Maximum checkpoints per run
              </label>
              <input
                id="delivery-limit"
                aria-describedby="delivery-limit-help"
                type="number"
                min={1}
                max={50}
                value={draft?.max_checkpoints ?? ""}
                onChange={(event) =>
                  draft &&
                  setDraft({
                    ...draft,
                    max_checkpoints: Number(event.target.value),
                  })
                }
              />
              <small id="delivery-limit-help" className="subtle">
                Includes the final verified checkpoint. Choose at least 2 for
                intermediate progress.
              </small>
            </div>
          </div>
          <label className="git-delivery-check">
            <input
              type="checkbox"
              checked={draft?.on_pause ?? false}
              onChange={(event) =>
                draft && setDraft({ ...draft, on_pause: event.target.checked })
              }
            />
            Also save progress at a safe pause
          </label>
          <div className="field">
            <label htmlFor="delivery-remote">Where to save progress</label>
            <select
              id="delivery-remote"
              value={draft?.remote ?? "local"}
              onChange={(event) =>
                draft &&
                setDraft({
                  ...draft,
                  remote: event.target.value as GitDeliveryPolicy["remote"],
                })
              }
            >
              <option value="local">Local Git checkpoints</option>
              <option value="draft_pr" disabled={!remoteAvailable}>
                Push to a draft review
              </option>
            </select>
          </div>
          {draft?.remote === "draft_pr" && (
            <p className="subtle">
              Push checkpoints to the repository and update its draft review.
              Final release remains separate.
            </p>
          )}
          {current && !current.permissions.publish && (
            <p className="subtle">
              Publishing progress requires current release permission.
            </p>
          )}
          {current?.remote_capabilities
            .filter((item) => !item.draft_updates)
            .map((item) => (
              <p className="subtle" key={item.repository_id}>
                {item.provider}:{" "}
                {item.reason ||
                  "This repository supports local checkpoints only."}
              </p>
            ))}
          <p className="subtle">
            Checkpoints record completed implementation rounds or a pause after
            the worker has stopped safely. They do not interrupt active work or
            replace final checks and review.
          </p>
        </fieldset>
        {current && !editable && (
          <p>
            A factory administrator with project membership-management
            permission can change this workflow.
          </p>
        )}
        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}
        {notice && <p role="status">{notice}</p>}
        <div className="integration-actions">
          <button
            className="secondary"
            type="button"
            disabled={busy}
            onClick={() => void load()}
          >
            <RefreshCw size={16} />
            Refresh workflow
          </button>
          <button
            className="primary"
            type="submit"
            disabled={
              busy ||
              !editable ||
              !valid ||
              !changed ||
              (draft?.enabled &&
                draft.remote === "draft_pr" &&
                !remoteAvailable)
            }
          >
            <Save size={16} />
            Save workflow
          </button>
        </div>
      </form>
    </section>
  );
}
