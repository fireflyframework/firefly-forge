/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState, type FormEvent } from "react";
import { ExternalLink, FolderGit2, RefreshCw } from "lucide-react";
import { post, request, type Connection } from "./api";

type SCMConnection = {
  id: string;
  name: string;
  auth_type: string;
  status: string;
  detail: string;
  setup_url?: string;
  installation_url?: string;
  permissions: string[];
  fields: {
    key: string;
    label: string;
    secret: boolean;
    required: boolean;
    placeholder: string;
  }[];
  config: Record<string, string | boolean>;
};
type RemoteRepository = {
  id: string;
  name: string;
  url: string;
  default_branch: string;
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : "Repository connection failed.";

export function SourceControl({
  connection,
  onConnected,
}: {
  connection: Connection;
  onConnected: () => void;
}) {
  const [catalog, setCatalog] = useState<SCMConnection[]>([]);
  const [provider, setProvider] = useState("github");
  const [repositories, setRepositories] = useState<RemoteRepository[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [clear, setClear] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const selected = catalog.find((item) => item.id === provider);
  useEffect(() => {
    const controller = new AbortController();
    request<SCMConnection[]>(connection, "/scm/connections", {
      signal: controller.signal,
    })
      .then((result) => setCatalog(Array.isArray(result) ? result : []))
      .catch((cause) => {
        if (!controller.signal.aborted) setError(message(cause));
      });
    return () => controller.abort();
  }, [connection]);
  function update(item: SCMConnection) {
    setCatalog((current) =>
      current.map((entry) => (entry.id === item.id ? item : entry)),
    );
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      update(
        await request<SCMConnection>(
          connection,
          `/scm/connections/${provider}`,
          {
            method: "PUT",
            body: JSON.stringify({ values, clear_secrets: clear }),
          },
        ),
      );
      setValues({});
      setClear([]);
      setRepositories([]);
      setNotice(
        "Application settings saved. Check access before choosing repositories.",
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function check() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await post<SCMConnection>(
        connection,
        `/scm/connections/${provider}/test`,
      );
      update(result);
      setNotice(result.detail);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function authorize() {
    setBusy(true);
    setError("");
    try {
      const result = await request<{ authorization_url: string }>(
        connection,
        `/scm/connections/${provider}/authorize`,
        { method: "POST", credentials: "include" },
      );
      const url = new URL(result.authorization_url);
      if (url.protocol !== "https:")
        throw new Error("Authorization requires a secure provider address.");
      window.location.assign(url.href);
    } catch (cause) {
      setError(message(cause));
      setBusy(false);
    }
  }
  async function discover() {
    setBusy(true);
    setError("");
    try {
      setRepositories(
        await request<RemoteRepository[]>(
          connection,
          `/scm/connections/${provider}/repositories`,
        ),
      );
      setNotice("The list contains repositories granted to this application.");
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function connect(id: string) {
    setBusy(true);
    setError("");
    try {
      await post(
        connection,
        `/scm/connections/${provider}/repositories/connect`,
        { repository_id: id },
      );
      setNotice("Repository connected. You can now start a run.");
      onConnected();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="source-control">
      <div
        className="integration-provider-choice"
        aria-label="Source control provider"
      >
        {catalog.map((item) => (
          <button
            type="button"
            key={item.id}
            className={
              provider === item.id ? "secondary selected" : "secondary"
            }
            aria-pressed={provider === item.id}
            onClick={() => {
              setProvider(item.id);
              setValues({});
              setClear([]);
              setRepositories([]);
              setError("");
              setNotice("");
            }}
          >
            {item.name}
          </button>
        ))}
      </div>
      {selected && (
        <article className="integration-card">
          <div>
            <h3>{selected.name}</h3>
            <p>{selected.detail}</p>
          </div>
          <div className="integration-actions">
            {selected.setup_url && (
              <a
                className="secondary"
                href={selected.setup_url}
                target="_blank"
                rel="noreferrer"
              >
                Create application <ExternalLink size={15} />
              </a>
            )}
            {selected.installation_url && (
              <a
                className="secondary"
                href={selected.installation_url}
                target="_blank"
                rel="noreferrer"
              >
                Install application <ExternalLink size={15} />
              </a>
            )}
          </div>
          <p>
            Grant access in {selected.name}. The factory uses the application's
            repository permissions for cloning and pull requests.
          </p>
          <form onSubmit={(event) => void save(event)}>
            {selected.fields.map((field) => (
              <div className="field" key={field.key}>
                <label htmlFor={`scm-${field.key}`}>{field.label}</label>
                {field.key === "private_key" ? (
                  <textarea
                    id={`scm-${field.key}`}
                    rows={5}
                    value={values[field.key] ?? ""}
                    placeholder={field.placeholder}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) =>
                      setValues({ ...values, [field.key]: event.target.value })
                    }
                    required={
                      field.required &&
                      !selected.config[field.key + "_configured"]
                    }
                  />
                ) : (
                  <input
                    id={`scm-${field.key}`}
                    type={field.secret ? "password" : "text"}
                    value={
                      values[field.key] ??
                      (field.secret
                        ? ""
                        : String(selected.config[field.key] ?? ""))
                    }
                    placeholder={field.placeholder}
                    autoComplete="off"
                    required={
                      field.required &&
                      !(
                        field.secret &&
                        selected.config[field.key + "_configured"]
                      )
                    }
                    onChange={(event) =>
                      setValues({ ...values, [field.key]: event.target.value })
                    }
                  />
                )}
                {field.secret && selected.config[field.key + "_configured"] && (
                  <>
                    <small>Stored on the server. Leave blank to keep it.</small>
                    <label className="check-choice">
                      <input
                        type="checkbox"
                        checked={clear.includes(field.key)}
                        onChange={(event) =>
                          setClear((current) =>
                            event.target.checked
                              ? [...current, field.key]
                              : current.filter((key) => key !== field.key),
                          )
                        }
                      />
                      Clear stored {field.label.toLowerCase()}
                    </label>
                  </>
                )}
              </div>
            ))}
            <div className="integration-actions">
              <button className="primary" disabled={busy}>
                Save application settings
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void check()}
              >
                Check access
              </button>
            </div>
          </form>
          <div className="integration-actions">
            {selected.auth_type.includes("oauth") && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void authorize()}
              >
                Authorize in {selected.name}
                <ExternalLink size={15} />
              </button>
            )}
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => void discover()}
            >
              <RefreshCw size={15} />
              Find granted repositories
            </button>
          </div>
        </article>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="integration-status" role="status">
          {notice}
        </p>
      )}
      {!!repositories.length && (
        <div className="integration-section">
          <h2>Choose repositories</h2>
          <div className="integration-grid">
            {repositories.map((item) => (
              <article className="integration-card" key={item.id}>
                <h3>
                  <FolderGit2 size={18} /> {item.name}
                </h3>
                <p>{item.url}</p>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => void connect(item.id)}
                >
                  Connect {item.name}
                </button>
              </article>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
