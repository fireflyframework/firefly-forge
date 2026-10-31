/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { Columns3, RefreshCw, X } from "lucide-react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ProjectSnapshot,
  type Repository,
} from "./api";
import type { IdentitySession } from "./Identity";
import {
  readOAuthDirectory,
  type OAuthSummary,
} from "./ConnectionOAuthSettings";
import {
  boardAccount,
  boardCreation,
  boardDiscovery,
  boardView,
  boardStates,
  pendingBoards,
  retainBoardRequest,
  type BoardAccount,
  type BoardContext,
  type BoardDiscovery,
  type BoardPending,
  type BoardView,
} from "./GitHubProjectsData";
import { GitHubProjectsWizard, type BoardDraft } from "./GitHubProjectsWizard";
import "./GitHubProjectsSettings.css";
const emptyDraft = (): BoardDraft => ({
  action: "connect",
  kind: "user",
  owner: "",
  number: "",
  title: "",
  createAck: false,
  field: "",
  options: {},
  enabled: true,
  includeExisting: false,
  manageAck: false,
});
function message(cause: unknown): string {
  if (cause instanceof ApiError) {
    if ([401, 403].includes(cause.status))
      return "Current access was refused. Refresh your session and project permissions before continuing.";
    if (cause.status === 404)
      return "GitHub Projects setup is unavailable here. Check the factory version and the selected project connection.";
    if ([409, 422].includes(cause.status))
      return "The server refused this setup. Refresh the current settings and check account, repository, and board access.";
  }
  return "The result could not be confirmed. Read the saved settings or creation receipt before sending another request.";
}
export function GitHubProjectsSettings({
  connection,
  session,
  project,
  repositories,
  onAccounts,
}: {
  connection: Connection;
  session: IdentitySession;
  project: ProjectSnapshot;
  repositories: Repository[];
  onAccounts?: () => void;
}) {
  const members = new Set(
    project.definition.components.map((item) => item.repository_id),
  );
  const choices = repositories.filter(
    (item) => members.has(item.id) && item.provider === "github",
  );
  const [selected, setSelected] = useState("");
  const repository = choices.find((item) => item.id === selected) ?? choices[0];
  const context: BoardContext = {
    base: new URL(
      normalizeBase(connection.base),
      window.location.href,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    project: project.id,
    repository: repository?.id ?? "",
  };
  const scope = JSON.stringify([
    context,
    connection.token,
    session.user.authentication_method,
    session.global_admin,
    session.memberships,
    project.digest,
  ]);
  return (
    <section
      className="integration-card github-projects-settings"
      aria-label="GitHub Projects"
    >
      <h3>
        <Columns3 size={20} aria-hidden="true" />
        GitHub Projects
      </h3>
      <p>
        Track verified issue and pull request progress on a private board using
        your separately connected GitHub account.
      </p>
      {!repository ? (
        <p>
          No GitHub repository belongs to this product project. Register one
          before setting up a board.
        </p>
      ) : (
        <>
          {choices.length > 1 && (
            <div className="field">
              <label htmlFor="board-repository">Board repository</label>
              <select
                id="board-repository"
                value={repository.id}
                onChange={(event) => setSelected(event.target.value)}
              >
                {choices.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <BoardPanel
            key={scope}
            connection={connection}
            context={context}
            repositoryName={repository.name}
            onAccounts={onAccounts}
          />
        </>
      )}
    </section>
  );
}
function BoardPanel({
  connection,
  context,
  repositoryName,
  onAccounts,
}: {
  connection: Connection;
  context: BoardContext;
  repositoryName: string;
  onAccounts?: () => void;
}) {
  const path = `/projects/${context.project}/repositories/${context.repository}/github-projects`;
  const [view, setView] = useState<BoardView | null>(null),
    [choices, setChoices] = useState<OAuthSummary[]>([]),
    [name, setName] = useState("");
  const [account, setAccount] = useState<BoardAccount | null>(null),
    [discovery, setDiscovery] = useState<BoardDiscovery | null>(null),
    [draft, setDraft] = useState<BoardDraft>(emptyDraft);
  const [markers, setMarkers] = useState<BoardPending[]>([]),
    [storageError, setStorageError] = useState("");
  const [open, setOpen] = useState(false),
    [step, setStep] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const alive = useRef(false),
    locked = useRef(false),
    active = useRef<AbortController | null>(null),
    dialog = useRef<HTMLDialogElement>(null),
    trigger = useRef<HTMLButtonElement>(null);
  const creationHeld = markers.some(
    (item) => item.kind === "create" && item.state === "unknown",
  );
  const configurationHeld = markers.some(
    (item) => item.kind === "configure" && item.state === "unknown",
  );
  function retain(marker: BoardPending) {
    const result = retainBoardRequest(localStorage, context, marker);
    setMarkers(result);
    return result;
  }
  async function operation(action: (signal: AbortSignal) => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    active.current = controller;
    try {
      await action(controller.signal);
    } catch (cause) {
      if (alive.current && !controller.signal.aborted) {
        setError(message(cause));
        if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
          setView(null);
          setAccount(null);
          setDiscovery(null);
          setChoices([]);
          setOpen(false);
        }
      }
    } finally {
      if (alive.current && !controller.signal.aborted) {
        setBusy(false);
        locked.current = false;
      }
    }
  }
  async function read(signal: AbortSignal, observeConfiguration = false) {
    const current = boardView(
      await request<unknown>(connection, path + "/connection", { signal }),
      context,
    );
    if (signal.aborted || !alive.current) return;
    setView(current);
    if (current.permissions.configure) {
      const directory = readOAuthDirectory(
        await request<unknown>(connection, "/connection-oauth", { signal }),
      );
      if (signal.aborted || !alive.current) return;
      setChoices(
        directory.filter(
          (item) =>
            ["github-projects", "github-projects-private"].includes(
              item.provider,
            ) &&
            item.project_id === context.project &&
            item.policy_current &&
            item.status === "authorized_unverified" &&
            !item.reconciliation_required,
        ),
      );
    } else {
      setChoices([]);
      setAccount(null);
      setDiscovery(null);
      setOpen(false);
    }
    if (observeConfiguration) {
      const pending = pendingBoards(localStorage, context).filter(
        (item) => item.kind === "configure" && item.state === "unknown",
      );
      for (const item of pending) retain({ ...item, state: "observed" });
      if (pending.length)
        setNotice(
          "Current saved settings were read. The original save result remains unconfirmed; its request ID is retained. No request was repeated.",
        );
    }
  }
  useEffect(() => {
    alive.current = true;
    try {
      setMarkers(pendingBoards(localStorage, context));
    } catch {
      setStorageError(
        "The retained board request history cannot be read. New changes are held; preserve browser storage for review.",
      );
    }
    void operation((signal) => read(signal));
    return () => {
      alive.current = false;
      active.current?.abort();
      locked.current = false;
    };
    // The parent remounts this scope for API, identity, permissions, project, or repository changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const node = dialog.current;
    if (!open || !node) return;
    node.showModal();
    return () => node.close();
  }, [open]);
  function close() {
    if (locked.current) return;
    setOpen(false);
    requestAnimationFrame(() => trigger.current?.focus());
  }
  function changeName(value: string) {
    setName(value);
    setAccount(null);
    setDiscovery(null);
    setDraft(emptyDraft());
    setNotice("");
  }
  function changeDraft(value: BoardDraft) {
    if (
      ["action", "kind", "owner", "number", "title"].some(
        (key) =>
          value[key as keyof BoardDraft] !== draft[key as keyof BoardDraft],
      )
    )
      setDiscovery(null);
    setDraft(value);
  }
  async function verify(signal: AbortSignal) {
    setAccount(null);
    setDiscovery(null);
    const proof = boardAccount(
      await request<unknown>(connection, path + "/account", {
        method: "POST",
        body: JSON.stringify({ version: 1, connection_name: name }),
        signal,
      }),
      context,
      name,
    );
    if (signal.aborted || !alive.current) return;
    setAccount(proof);
    setDraft({ ...emptyDraft(), owner: proof.viewer.login });
  }
  async function discover(
    signal: AbortSignal,
    proof: BoardAccount,
    number: number,
    kind: BoardDraft["kind"],
    owner: string,
  ) {
    setDiscovery(null);
    const found = boardDiscovery(
      await request<unknown>(connection, path + "/discover", {
        method: "POST",
        body: JSON.stringify({
          version: 1,
          connection_name: proof.connection_name,
          owner_kind: kind,
          owner,
          number,
        }),
        signal,
      }),
      proof,
    );
    if (
      found.board.number !== number ||
      found.board.owner_kind !== kind ||
      found.board.owner.toLowerCase() !== owner.toLowerCase()
    )
      throw new Error("Board changed");
    if (signal.aborted || !alive.current) return;
    setDiscovery(found);
    const binding = view?.binding,
      same = binding?.board_id === found.board.id;
    setDraft((current) => ({
      ...current,
      number: String(number),
      field: same ? binding.field_id : "",
      options: same ? binding.state_options : {},
      enabled: binding?.enabled ?? true,
      includeExisting: binding?.include_existing ?? false,
      manageAck: false,
    }));
  }
  async function create(signal: AbortSignal) {
    if (
      !account ||
      !view?.permissions.configure ||
      creationHeld ||
      storageError ||
      !draft.createAck
    )
      return;
    const marker: BoardPending = {
      id: crypto.randomUUID().replaceAll("-", ""),
      kind: "create",
      name: account.connection_name,
      owner: account.viewer.login,
      title: draft.title,
      state: "unknown",
    };
    retain(marker);
    const receipt = boardCreation(
      await request<unknown>(connection, path + "/creations", {
        method: "POST",
        body: JSON.stringify({
          version: 1,
          request_id: marker.id,
          connection_name: marker.name,
          owner_kind: "user",
          owner: marker.owner,
          title: marker.title,
          acknowledge_private_creation: true,
        }),
        signal,
      }),
      context,
      marker,
    );
    if (signal.aborted || !alive.current) return;
    if (!receipt.board) {
      setNotice(
        "Creation remains unknown. Read the retained receipt; another creation is held.",
      );
      return;
    }
    retain({ ...marker, state: "observed" });
    await discover(
      signal,
      account,
      receipt.board.number,
      "user",
      receipt.board.owner,
    );
    setStep(2);
  }
  async function save(signal: AbortSignal) {
    if (
      !account ||
      !discovery ||
      !view?.permissions.configure ||
      configurationHeld ||
      storageError ||
      !draft.manageAck
    )
      return;
    const marker: BoardPending = {
      id: crypto.randomUUID().replaceAll("-", ""),
      kind: "configure",
      name: account.connection_name,
      owner: discovery.board.owner,
      title: discovery.board.title,
      state: "unknown",
    };
    const body = {
      version: 1,
      request_id: marker.id,
      expected_digest: view.binding?.digest ?? null,
      connection_name: marker.name,
      owner_kind: discovery.board.owner_kind,
      owner: marker.owner,
      number: discovery.board.number,
      board_id: discovery.board.id,
      field_id: draft.field,
      state_options: draft.options,
      enabled: draft.enabled,
      include_existing: draft.includeExisting,
      acknowledge_managed_field: true,
    };
    retain(marker);
    const saved = boardView(
      await request<unknown>(connection, path + "/connection", {
        method: "PUT",
        body: JSON.stringify(body),
        signal,
      }),
      context,
    );
    if (
      !saved.binding ||
      saved.binding.board_id !== body.board_id ||
      saved.binding.field_id !== body.field_id ||
      boardStates.some(
        (state) =>
          saved.binding!.state_options[state] !== body.state_options[state],
      ) ||
      saved.binding.connection_name !== body.connection_name ||
      saved.binding.owner_kind !== body.owner_kind ||
      saved.binding.owner.toLowerCase() !== body.owner.toLowerCase() ||
      saved.binding.number !== body.number ||
      saved.binding.enabled !== body.enabled ||
      saved.binding.include_existing !== body.include_existing
    )
      throw new Error("Saved board differs");
    if (signal.aborted || !alive.current) return;
    retain({ ...marker, state: "observed" });
    setView(saved);
    setNotice(
      "Board settings saved. Verified progress will use the selected field and options.",
    );
    setOpen(false);
    requestAnimationFrame(() => trigger.current?.focus());
  }
  async function receipt(signal: AbortSignal, marker: BoardPending) {
    const result = boardCreation(
      await request<unknown>(connection, path + "/creations/" + marker.id, {
        signal,
      }),
      context,
      marker,
    );
    if (signal.aborted || !alive.current) return;
    if (result.status === "completed") {
      retain({ ...marker, state: "observed" });
      setNotice(
        `The retained creation receipt confirms ${result.board!.title} (#${result.board!.number}) under ${result.board!.owner}. Open setup to verify and connect this board number.`,
      );
    } else
      setNotice(
        "Creation remains unknown. Attribution is unknown and replay is not authorized. You may connect an existing board while retaining this request.",
      );
  }
  return (
    <>
      <p className="feature-meta">
        {repositoryName} ·{" "}
        {view
          ? view.binding
            ? `${view.binding.enabled ? "Synchronization enabled" : "Synchronization disabled"} · ${view.status.replaceAll("_", " ")}`
            : "No private board configured"
          : "Current board settings are unavailable"}
      </p>
      {view?.binding && (
        <p>
          <a
            href={view.binding.board_url}
            target="_blank"
            rel="noreferrer noopener"
          >
            Open saved board
          </a>{" "}
          · configuration revision {view.binding.revision}
        </p>
      )}
      {view?.latest_request && (
        <p className="feature-meta">
          Latest synchronization: {view.latest_request.status} · request{" "}
          <code>{view.latest_request.id}</code>
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {(storageError || (!open && error)) && (
        <div role="alert" className="alert">
          {storageError || error}
        </div>
      )}
      <div className="board-actions">
        <button
          type="button"
          ref={trigger}
          className="secondary"
          disabled={
            busy ||
            !view?.permissions.configure ||
            !!storageError ||
            configurationHeld
          }
          onClick={() => {
            setStep(0);
            setName(view?.binding?.connection_name ?? "");
            setAccount(null);
            setDiscovery(null);
            setDraft(emptyDraft());
            setOpen(true);
            setError("");
          }}
        >
          Set up GitHub Projects
        </button>
        <button
          type="button"
          className="quiet"
          disabled={busy}
          onClick={() => void operation((signal) => read(signal, true))}
        >
          <RefreshCw size={16} aria-hidden="true" />
          Refresh board settings
        </button>
      </div>
      {view && !view.permissions.configure && (
        <p className="subtle">
          Setup requires current factory administrator access and release
          permission for this project and repository.
        </p>
      )}
      {markers
        .filter((item) => item.state === "unknown")
        .map((item) => (
          <div className="board-pending" key={item.id}>
            <p>
              {item.kind === "create"
                ? "Unconfirmed private creation"
                : "Unconfirmed settings save"}{" "}
              · request <code>{item.id}</code>
            </p>
            {item.kind === "create" ? (
              <button
                type="button"
                className="secondary"
                disabled={busy || !view}
                onClick={() =>
                  void operation((signal) => receipt(signal, item))
                }
              >
                Read creation receipt
              </button>
            ) : (
              <p>
                Refresh board settings to inspect the current configuration. The
                original save will not be sent again.
              </p>
            )}
          </div>
        ))}
      <dialog
        ref={dialog}
        className="feature-dialog source-analysis-dialog github-projects-dialog"
        aria-label="GitHub Projects setup"
        onCancel={(event) => {
          event.preventDefault();
          close();
        }}
      >
        <button
          type="button"
          className="quiet source-dialog-close"
          onClick={close}
          disabled={busy}
          aria-label="Close GitHub Projects setup"
        >
          <X size={18} aria-hidden="true" />
          Close
        </button>
        {open && (
          <GitHubProjectsWizard
            step={step}
            onStep={setStep}
            busy={busy}
            choices={choices}
            name={name}
            onName={changeName}
            account={account}
            discovery={discovery}
            draft={draft}
            onDraft={changeDraft}
            onVerify={() => void operation(verify)}
            onDiscover={() => {
              if (account)
                void operation((signal) =>
                  discover(
                    signal,
                    account,
                    Number(draft.number),
                    draft.kind,
                    draft.kind === "user" ? account.viewer.login : draft.owner,
                  ),
                );
            }}
            onCreate={() => void operation(create)}
            onSave={() => void operation(save)}
            creationHeld={creationHeld || !!storageError}
            configurationHeld={configurationHeld || !!storageError}
            onAccounts={onAccounts}
            error={error}
          />
        )}
      </dialog>
    </>
  );
}
