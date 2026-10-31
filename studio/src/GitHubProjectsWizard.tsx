/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { ArrowRight, ExternalLink } from "lucide-react";
import { Wizard } from "./Wizard";
import {
  boardStates,
  type BoardAccount,
  type BoardDiscovery,
} from "./GitHubProjectsData";
import type { OAuthSummary } from "./ConnectionOAuthSettings";
export type BoardDraft = {
  action: "connect" | "create";
  kind: "user" | "organization";
  owner: string;
  number: string;
  title: string;
  createAck: boolean;
  field: string;
  options: Record<string, string>;
  enabled: boolean;
  includeExisting: boolean;
  manageAck: boolean;
};
export const stateLabel = (state: string) =>
  state[0].toUpperCase() + state.slice(1).replaceAll("_", " ");
export function GitHubProjectsWizard({
  step,
  onStep,
  busy,
  choices,
  name,
  onName,
  account,
  discovery,
  draft,
  onDraft,
  onVerify,
  onDiscover,
  onCreate,
  onSave,
  creationHeld,
  configurationHeld,
  onAccounts,
  error,
}: {
  step: number;
  onStep: (step: number) => void;
  busy: boolean;
  choices: OAuthSummary[];
  name: string;
  onName: (name: string) => void;
  account: BoardAccount | null;
  discovery: BoardDiscovery | null;
  draft: BoardDraft;
  onDraft: (draft: BoardDraft) => void;
  onVerify: () => void;
  onDiscover: () => void;
  onCreate: () => void;
  onSave: () => void;
  creationHeld: boolean;
  configurationHeld: boolean;
  onAccounts?: () => void;
  error: string;
}) {
  const field = discovery?.fields.find((item) => item.id === draft.field);
  const chosen = boardStates.filter((state) =>
    field?.options.some((option) => option.id === draft.options[state]),
  ).length;
  const complete =
    !!field &&
    boardStates.every((state) =>
      field.options.some((option) => option.id === draft.options[state]),
    );
  const canCreate =
    !!account &&
    draft.action === "create" &&
    !!draft.title.trim() &&
    draft.title.length <= 256 &&
    draft.createAck &&
    !creationHeld;
  return (
    <Wizard
      variant="rail"
      layout="flow"
      className={step === 2 ? "board-status-wizard" : ""}
      stepDescriptions={[
        "Verify the connected account",
        "Find or create a private board",
        "Choose the field and eight mappings",
      ]}
      steps={["Account", "Private board", "Board statuses"]}
      active={step}
      onStep={onStep}
      locked={busy}
      title={
        [
          "Verify your GitHub account",
          "Choose a private board",
          "Map board statuses",
        ][step]
      }
      description={
        [
          "Use a separately consented connection for this project. Repository GitHub App permissions stay separate.",
          "Check a private board you can update, or explicitly create a private personal board.",
          "",
        ][step]
      }
      headingAction={
        step === 2 ? (
          <span
            className="board-status-progress"
            role="status"
            aria-label={`${chosen} of 8 statuses chosen`}
          >
            {chosen}/8
          </span>
        ) : undefined
      }
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (step === 0) onVerify();
          else if (step === 1) {
            if (draft.action === "create") {
              if (canCreate) onCreate();
            } else onDiscover();
          } else if (complete && draft.manageAck) onSave();
        }}
      >
        {error && (
          <div role="alert" className="alert">
            {error}
          </div>
        )}
        <fieldset disabled={busy}>
          <legend className="sr-only">
            {
              ["GitHub account", "Private board selection", "Board statuses"][
                step
              ]
            }
          </legend>
          {step === 0 && (
            <>
              <div className="field">
                <label htmlFor="board-oauth">Project OAuth connection</label>
                <select
                  id="board-oauth"
                  value={name}
                  onChange={(event) => onName(event.target.value)}
                >
                  <option value="">Choose a consented connection</option>
                  {choices.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name} ·{" "}
                      {item.provider === "github-projects-private"
                        ? "Projects and private repositories"
                        : "Projects and public repositories"}
                    </option>
                  ))}
                </select>
                <small>
                  Only current GitHub Projects connections scoped to this
                  project are listed. Account verification checks who actually
                  consented.
                </small>
              </div>
              {!choices.length && (
                <p>
                  No eligible connection is available. A factory administrator
                  can register a GitHub OAuth App and authorize it in Connected
                  accounts.
                </p>
              )}
              {onAccounts && (
                <button
                  type="button"
                  className="secondary"
                  onClick={onAccounts}
                >
                  Open Connected accounts
                </button>
              )}
              <button type="submit" className="secondary" disabled={!name}>
                Verify connected account
              </button>
              {account && (
                <div className="board-proof" role="status">
                  <strong>Connected as {account.viewer.login}</strong>
                  <span>
                    {account.repository.name_with_owner} ·{" "}
                    {account.repository.private
                      ? "Private repository access verified"
                      : "Public repository access verified"}
                  </span>
                  <small>
                    Creating a personal board uses this account. Organization
                    boards require their current update permission.
                  </small>
                </div>
              )}
            </>
          )}
          {step === 1 && account && (
            <>
              <p>
                Connected as <strong>{account.viewer.login}</strong> ·{" "}
                {account.repository.name_with_owner}
              </p>
              <div className="field">
                <label htmlFor="board-action">Board action</label>
                <select
                  id="board-action"
                  value={draft.action}
                  onChange={(event) =>
                    onDraft({
                      ...draft,
                      action: event.target.value as BoardDraft["action"],
                      createAck: false,
                    })
                  }
                >
                  <option value="connect">
                    Connect an existing private board
                  </option>
                  <option value="create">
                    Create a private personal board
                  </option>
                </select>
              </div>
              {draft.action === "connect" ? (
                <>
                  <div className="board-fields">
                    <div className="field">
                      <label htmlFor="board-owner-kind">Board owner type</label>
                      <select
                        id="board-owner-kind"
                        value={draft.kind}
                        onChange={(event) =>
                          onDraft({
                            ...draft,
                            kind: event.target.value as BoardDraft["kind"],
                            owner: account.viewer.login,
                          })
                        }
                      >
                        <option value="user">My personal account</option>
                        <option value="organization">
                          An organization I can update
                        </option>
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="board-owner">
                        {draft.kind === "user"
                          ? "Verified personal owner"
                          : "Organization login"}
                      </label>
                      <input
                        id="board-owner"
                        value={
                          draft.kind === "user"
                            ? account.viewer.login
                            : draft.owner
                        }
                        readOnly={draft.kind === "user"}
                        onChange={(event) =>
                          onDraft({ ...draft, owner: event.target.value })
                        }
                        autoComplete="off"
                      />
                    </div>
                  </div>
                  <div className="field">
                    <label htmlFor="board-number">Board number</label>
                    <input
                      id="board-number"
                      type="number"
                      min={1}
                      max={2147483647}
                      value={draft.number}
                      onChange={(event) =>
                        onDraft({ ...draft, number: event.target.value })
                      }
                    />
                    <small>
                      The number at the end of the GitHub Projects board URL.
                    </small>
                  </div>
                  <button
                    type="submit"
                    className="secondary"
                    disabled={
                      !draft.owner.trim() ||
                      !Number.isSafeInteger(Number(draft.number)) ||
                      Number(draft.number) < 1
                    }
                  >
                    Find private board
                  </button>
                </>
              ) : (
                <>
                  <div className="field">
                    <label htmlFor="board-title">Board title</label>
                    <input
                      id="board-title"
                      value={draft.title}
                      maxLength={256}
                      onChange={(event) =>
                        onDraft({
                          ...draft,
                          title: event.target.value,
                          createAck: false,
                        })
                      }
                    />
                  </div>
                  <p>
                    The new board belongs to{" "}
                    <strong>{account.viewer.login}</strong> and is private.
                    Creating it does not add issues or change your repository
                    permissions.
                  </p>
                  <label className="board-check">
                    <input
                      type="checkbox"
                      checked={draft.createAck}
                      onChange={(event) =>
                        onDraft({ ...draft, createAck: event.target.checked })
                      }
                    />
                    Create a private personal board under {account.viewer.login}
                    .
                  </label>
                  {creationHeld && (
                    <p role="status">
                      An earlier creation is unconfirmed. Read its receipt
                      before considering another board. You can still connect an
                      existing board.
                    </p>
                  )}
                  <button
                    type="submit"
                    className="primary"
                    disabled={!canCreate}
                  >
                    Create private personal board
                  </button>
                </>
              )}
              {discovery && (
                <div className="board-proof" role="status">
                  <strong>{discovery.board.title}</strong>
                  <span>
                    Private · current update permission verified ·{" "}
                    {discovery.board.owner}
                  </span>
                  <a
                    href={discovery.board.url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    Open verified board{" "}
                    <ExternalLink size={14} aria-hidden="true" />
                  </a>
                </div>
              )}
            </>
          )}
          {step === 2 && discovery && account && (
            <>
              <div className="field">
                <p id="board-field-context" className="board-destination">
                  <strong>{discovery.board.title}</strong>
                  <span>
                    {account.repository.name_with_owner} · Private board
                  </span>
                </p>
                <label htmlFor="board-field">Managed field</label>
                <select
                  id="board-field"
                  aria-describedby="board-field-context"
                  value={draft.field}
                  onChange={(event) =>
                    onDraft({
                      ...draft,
                      field: event.target.value,
                      options: {},
                      manageAck: false,
                    })
                  }
                >
                  <option value="">Choose a single-select field</option>
                  {discovery.fields.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="board-fields">
                {boardStates.map((state) => (
                  <div className="field" key={state}>
                    <label htmlFor={`board-state-${state}`}>
                      {stateLabel(state)}
                    </label>
                    <select
                      id={`board-state-${state}`}
                      aria-describedby={
                        state === "succeeded"
                          ? "board-completed-review"
                          : undefined
                      }
                      value={draft.options[state] ?? ""}
                      disabled={!field}
                      onChange={(event) =>
                        onDraft({
                          ...draft,
                          options: {
                            ...draft.options,
                            [state]: event.target.value,
                          },
                          manageAck: false,
                        })
                      }
                    >
                      <option value="">Choose an option</option>
                      {field?.options.map((option) => (
                        <option key={option.id} value={option.id}>
                          {option.name}
                        </option>
                      ))}
                    </select>
                    {state === "succeeded" && (
                      <small id="board-completed-review">
                        Completed work still needs product review. Choose In
                        review or your board’s equivalent.
                      </small>
                    )}
                  </div>
                ))}
              </div>
              <p className="subtle">
                Human field changes are held as conflicts.
              </p>
              <label className="board-check">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={(event) =>
                    onDraft({
                      ...draft,
                      enabled: event.target.checked,
                      manageAck: false,
                    })
                  }
                />
                Synchronize future verified progress
              </label>
              <label className="board-check">
                <input
                  type="checkbox"
                  checked={draft.includeExisting}
                  onChange={(event) =>
                    onDraft({
                      ...draft,
                      includeExisting: event.target.checked,
                      manageAck: false,
                    })
                  }
                />
                Include runs recorded before the first configuration
              </label>
              <label className="board-check">
                <input
                  type="checkbox"
                  checked={draft.manageAck}
                  onChange={(event) =>
                    onDraft({ ...draft, manageAck: event.target.checked })
                  }
                />
                Allow forge to update only this field for linked issues and pull
                requests.
              </label>
            </>
          )}
        </fieldset>
        <div className="wizard-actions">
          {step > 0 && (
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => onStep(step - 1)}
            >
              Back
            </button>
          )}
          {step < 2 ? (
            <button
              type="button"
              className="primary"
              disabled={busy || (step === 0 ? !account : !discovery)}
              onClick={() => onStep(step + 1)}
            >
              Continue <ArrowRight size={16} aria-hidden="true" />
            </button>
          ) : (
            <button
              type="submit"
              className="primary"
              disabled={
                busy || !complete || !draft.manageAck || configurationHeld
              }
            >
              Save board settings
            </button>
          )}
        </div>
      </form>
    </Wizard>
  );
}
