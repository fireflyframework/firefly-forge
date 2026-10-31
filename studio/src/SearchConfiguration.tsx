/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Globe,
  KeyRound,
  Loader2,
  RefreshCw,
  Settings2,
  ShieldCheck,
} from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import {
  readSearchSettings,
  searchError,
  searchPayload,
  type SearchDraft,
  type SearchSettings,
} from "./SearchSettings";
import "./SearchConfiguration.css";

const providerName = (provider: SearchSettings["provider"]) =>
  provider === "tavily" ? "Tavily" : "Brave";
export function SearchConfiguration({
  connection,
  session,
}: {
  connection: Connection;
  session: IdentitySession | null;
}) {
  return (
    <>
      <div className="page-heading search-page-heading">
        <div>
          <h1>Internet search</h1>
        </div>
        <Globe size={26} />
      </div>
      {session?.global_admin ? (
        <SearchAdministrator
          key={`${connection.base}:${session.user.id}`}
          connection={connection}
        />
      ) : (
        <section className="access-notice">
          <ShieldCheck size={22} />
          <div>
            <h2>Factory administrator access required</h2>
            <p>
              Project membership does not grant access to search keys, settings
              or connection tests. Ask a factory administrator to configure this
              optional service.
            </p>
          </div>
        </section>
      )}
    </>
  );
}
function SearchAdministrator({ connection }: { connection: Connection }) {
  const [saved, setSaved] = useState<SearchSettings | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [uncertain, setUncertain] = useState(false),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0);
  const [draft, setDraft] = useState<SearchDraft>({
    provider: "tavily",
    key: "",
    domains: "",
    remove: false,
  });
  const [review, setReview] = useState<Record<string, unknown>>({});
  const active = useRef<AbortController | null>(null),
    alert = useRef<HTMLDivElement>(null);
  const readSaved = useCallback(
    async (controller: AbortController) => {
      const value = readSearchSettings(
        await request<unknown>(connection, "/search/configuration", {
          signal: controller.signal,
        }),
      );
      if (controller.signal.aborted) return null;
      setSaved(value);
      return value;
    },
    [connection],
  );
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    void readSaved(controller)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(searchError(cause, ""));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => active.current?.abort();
  }, [readSaved]);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  async function reload() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await readSaved(controller);
      if (value) {
        setUncertain(false);
        setEditing(false);
        setDraft({
          provider: value.provider,
          key: "",
          domains: "",
          remove: false,
        });
        setReview({});
        setNotice(
          "Saved settings reloaded. Review them before making another request.",
        );
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(searchError(cause, draft.key));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function edit() {
    if (!saved || busy || uncertain) return;
    setDraft({
      provider: saved.revision ? saved.provider : "tavily",
      key: "",
      domains: (saved.include_domains ?? []).join("\n"),
      remove: false,
    });
    setReview({});
    setStep(0);
    setError("");
    setNotice("");
    setEditing(true);
  }
  function move(next: number) {
    setError("");
    setStep(next);
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!saved || busy || uncertain) return;
    setError("");
    if (step === 0) {
      move(1);
      return;
    }
    if (step === 1) {
      try {
        const values = searchPayload(saved, draft);
        setReview({ ...values, api_key: Boolean(values.api_key) });
        move(2);
      } catch (cause) {
        setError(searchError(cause, draft.key));
      }
      return;
    }
    let values: Record<string, unknown>;
    try {
      values = searchPayload(saved, draft);
    } catch (cause) {
      setStep(1);
      setError(searchError(cause, draft.key));
      return;
    }
    if (!Object.keys(values).length) {
      setNotice("No settings changed.");
      setEditing(false);
      setDraft({ ...draft, key: "" });
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setNotice("");
    try {
      const value = readSearchSettings(
        await request<unknown>(connection, "/search/configuration", {
          method: "PUT",
          body: JSON.stringify(values),
          signal: controller.signal,
        }),
      );
      if (controller.signal.aborted) return;
      setSaved(value);
      setEditing(false);
      setNotice(
        "Search settings saved. Test the saved connection when you are ready to use provider quota.",
      );
    } catch (cause) {
      if (controller.signal.aborted) return;
      const known =
        cause instanceof ApiError &&
        cause.status >= 400 &&
        cause.status < 500 &&
        cause.status !== 408;
      setUncertain(!known);
      if (known) {
        setStep(1);
        setReview({});
      }
      setError(
        (known
          ? ""
          : "The save outcome is not confirmed. Reload saved settings before trying again. ") +
          searchError(cause, draft.key),
      );
    } finally {
      if (!controller.signal.aborted) {
        setDraft((current) => ({ ...current, key: "" }));
        setBusy(false);
      }
    }
  }
  async function testConnection() {
    if (!saved?.configured || busy || uncertain || editing) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await request<unknown>(connection, "/search/test", {
        method: "POST",
        signal: controller.signal,
      });
      // The test receipt does not identify a revision; read the current saved status.
      const value = await readSaved(controller);
      if (value)
        setNotice(
          value.live_search_verified
            ? "The saved connection is verified for this revision."
            : "The test returned, but the current revision is not verified. Review saved settings before requesting another test.",
        );
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          searchError(cause, "") +
            " Review provider usage before another test; a failed request may still consume quota.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <div className="search-configuration">
      {error && (
        <div className="alert" role="alert" tabIndex={-1} ref={alert}>
          {error}
        </div>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {!saved && (
        <section className="panel search-state">
          <h2>
            {busy ? "Reading saved settings" : "Search settings unavailable"}
          </h2>
          <p>
            Configuration stays on the factory server. Reading settings does not
            contact the search provider.
          </p>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void reload()}
          >
            <RefreshCw size={16} />
            Reload saved settings
          </button>
        </section>
      )}
      {uncertain && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void reload()}
        >
          <RefreshCw size={16} />
          Reload saved settings
        </button>
      )}
      {saved && !editing && (
        <section className="panel search-state">
          <div className="section-heading">
            <div>
              <h2>{providerName(saved.provider)} connection</h2>
              <p>
                {saved.configured
                  ? "A provider key is saved privately on this factory."
                  : "No provider key is saved for new search requests."}
              </p>
            </div>
            <span
              className={
                "badge " +
                (saved.configured && saved.live_search_verified
                  ? "success"
                  : "")
              }
            >
              {!saved.configured
                ? "Not configured"
                : saved.live_search_verified
                  ? "Verified for this revision"
                  : "Not tested"}
            </span>
          </div>
          <dl className="wizard-summary">
            <div>
              <dt>Allowed websites</dt>
              <dd>
                {saved.include_domains?.length
                  ? saved.include_domains.join("\n")
                  : "Any admitted public website"}
              </dd>
            </div>
            <div>
              <dt>Saved revision</dt>
              <dd>{saved.revision ?? "No settings saved yet"}</dd>
            </div>
          </dl>
          <div className="search-actions">
            <button
              className="primary"
              disabled={busy || uncertain}
              onClick={edit}
            >
              <Settings2 size={16} />
              {saved.revision
                ? "Edit search settings"
                : "Set up internet search"}
            </button>
            <button
              className="secondary"
              disabled={!saved.configured || busy || uncertain}
              onClick={() => void testConnection()}
            >
              {busy ? (
                <Loader2 size={16} className="spin" />
              ) : (
                <ShieldCheck size={16} />
              )}
              Test saved connection
            </button>
          </div>
          <p className="search-quota">
            Testing sends one real search request and may use paid provider
            quota. Saving settings does not run a test.
          </p>
          <details>
            <summary>How agents use this connection</summary>
            <p>
              New runs use the saved provider, key revision and website
              restrictions. Already queued runs keep their frozen settings.
              Search also needs permission in the execution role and the
              repository runner. Changing this connection does not grant those
              permissions.
            </p>
          </details>
        </section>
      )}
      {saved && editing && (
        <Wizard
          className="search-wizard"
          steps={["Provider", "Access", "Review"]}
          active={step}
          onStep={move}
          locked={busy || uncertain}
          title={
            [
              "Choose a search provider",
              "Add provider access",
              "Review search settings",
            ][step]
          }
          description={
            [
              "Choose the account this factory will use for public source searches.",
              "Enter a new key only when creating or changing the connection.",
              "Saving does not contact the provider. A separate connection test may use paid quota.",
            ][step]
          }
          guide={
            <>
              <Globe size={24} />
              <h3>
                {step === 0
                  ? "Your provider account"
                  : step === 1
                    ? "Keys stay private"
                    : "New runs use these settings"}
              </h3>
              <p>
                {step === 0
                  ? "Tavily is recommended for a new connection. You can keep an existing Brave connection or choose either provider with its own account."
                  : step === 1
                    ? "The factory stores the key and never returns it to Studio. Coding agents receive citations without the provider key."
                    : "Saving resets the connection test status. Already queued runs retain their saved credential revision."}
              </p>
              <p>
                Search is optional. Provider accounts and quota are managed with
                the provider.
              </p>
            </>
          }
        >
          <form onSubmit={(event) => void submit(event)}>
            {step === 0 && (
              <div className="field">
                <label htmlFor="search-provider">Search provider</label>
                <select
                  id="search-provider"
                  value={draft.provider}
                  disabled={busy || uncertain}
                  onChange={(event) => {
                    setDraft({
                      ...draft,
                      provider: event.target
                        .value as SearchSettings["provider"],
                      key: "",
                      remove: false,
                    });
                    setError("");
                  }}
                >
                  <option value="tavily">
                    Tavily
                    {!saved.revision ? " (recommended for new setup)" : ""}
                  </option>
                  <option value="brave">Brave</option>
                </select>
                <small>
                  {draft.provider !== saved.provider && saved.revision
                    ? "Changing providers requires a new key for the selected provider. The saved key will not be transferred."
                    : saved.configured
                      ? "Keeping this provider preserves its saved key unless you enter a replacement."
                      : "Obtain a key from the provider account you choose."}
                </small>
              </div>
            )}
            {step === 1 && (
              <>
                <div className="field">
                  <label htmlFor="search-key">
                    <KeyRound size={16} /> Provider key
                  </label>
                  <input
                    id="search-key"
                    type="password"
                    value={draft.key}
                    maxLength={4096}
                    autoComplete="off"
                    spellCheck={false}
                    disabled={busy || uncertain || draft.remove}
                    onChange={(event) =>
                      setDraft({ ...draft, key: event.target.value })
                    }
                    aria-describedby="search-key-hint"
                    placeholder={
                      saved.configured && draft.provider === saved.provider
                        ? "Leave blank to keep the saved key"
                        : "Enter a key for " + providerName(draft.provider)
                    }
                  />
                  <small id="search-key-hint">
                    {draft.provider !== saved.provider
                      ? "Enter a new key from " +
                        providerName(draft.provider) +
                        ". The server rejects keys saved for another provider."
                      : saved.configured
                        ? "The saved key is never displayed. Leave this blank to keep it, or enter a replacement."
                        : "A provider key is required to create this connection."}
                  </small>
                </div>
                <div className="field">
                  <label htmlFor="search-domains">Allowed websites</label>
                  <textarea
                    id="search-domains"
                    rows={2}
                    maxLength={2600}
                    value={draft.domains}
                    disabled={busy || uncertain}
                    onChange={(event) =>
                      setDraft({ ...draft, domains: event.target.value })
                    }
                    aria-describedby="search-domains-hint"
                    placeholder={"fireflyframework.org\ngithub.com"}
                  />
                  <small id="search-domains-hint">
                    Optional. Enter one public hostname per line, up to ten.
                    Subdomains are included. Leave blank to allow any admitted
                    public website.
                  </small>
                </div>
                {saved.configured && draft.provider === saved.provider && (
                  <details className="search-removal">
                    <summary>Remove the saved key</summary>
                    <p>
                      This disables new search use. Historical keys remain
                      available to already queued runs.
                    </p>
                    <label className="check-choice">
                      <input
                        type="checkbox"
                        checked={draft.remove}
                        disabled={busy || uncertain}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            remove: event.target.checked,
                            key: "",
                          })
                        }
                      />
                      Remove the active key when I save
                    </label>
                  </details>
                )}
              </>
            )}
            {step === 2 && (
              <>
                <dl className="wizard-summary">
                  <div>
                    <dt>Provider</dt>
                    <dd>
                      {providerName(draft.provider)}
                      {saved.revision && draft.provider !== saved.provider
                        ? " (changed)"
                        : ""}
                    </dd>
                  </div>
                  <div>
                    <dt>Provider key</dt>
                    <dd>
                      {draft.remove
                        ? "Remove active key"
                        : review.api_key
                          ? "New key entered"
                          : "Keep saved key"}
                    </dd>
                  </div>
                  <div>
                    <dt>Allowed websites</dt>
                    <dd>
                      {(
                        (review.include_domains ??
                          saved.include_domains ??
                          []) as string[]
                      ).join("\n") || "Any admitted public website"}
                    </dd>
                  </div>
                </dl>
              </>
            )}
            <div className="wizard-actions">
              <div>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || uncertain}
                  onClick={() => {
                    setDraft({ ...draft, key: "" });
                    setEditing(false);
                    setError("");
                  }}
                >
                  Cancel
                </button>
                {step > 0 && (
                  <button
                    type="button"
                    className="quiet"
                    disabled={busy || uncertain}
                    onClick={() => move(step - 1)}
                  >
                    <ArrowLeft size={16} />
                    Back
                  </button>
                )}
              </div>
              <button className="primary" disabled={busy || uncertain}>
                {busy ? (
                  <Loader2 size={16} className="spin" />
                ) : step === 2 ? (
                  <Check size={16} />
                ) : (
                  <ArrowRight size={16} />
                )}{" "}
                {step === 2 ? "Save search settings" : "Continue"}
              </button>
            </div>
          </form>
        </Wizard>
      )}
    </div>
  );
}
