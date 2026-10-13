/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { request, type Connection } from "./api";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
export type DeploymentCredential = { id: string; configured_keys: string[] };
export function CredentialProfile({
  connection,
  keys,
  initial,
  onSaved,
  onClose,
}: {
  connection: Connection;
  keys: string[];
  initial: string;
  onSaved: (profile: DeploymentCredential) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [step, setStep] = useState(0);
  const [id, setId] = useState(initial);
  const [profiles, setProfiles] = useState<DeploymentCredential[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [clear, setClear] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    const controller = new AbortController();
    request<DeploymentCredential[]>(connection, "/deployment-credentials", {
      signal: controller.signal,
    })
      .then(setProfiles)
      .catch((cause) => {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Profile catalog unavailable.",
          );
      });
    return () => {
      controller.abort();
      element?.close();
    };
  }, [connection]);
  const configured =
    profiles.find((profile) => profile.id === id)?.configured_keys ?? [];
  const accountKeys = keys.filter(
    (key) =>
      ![
        "SSH_AUTH_SOCK",
        "ANSIBLE_PRIVATE_KEY_FILE",
        "SSH_KNOWN_HOSTS_FILE",
        "DOCKER_CONFIG",
      ].includes(key),
  );
  const hostKeys = [
    "SSH_AUTH_SOCK",
    "ANSIBLE_PRIVATE_KEY_FILE",
    "SSH_KNOWN_HOSTS_FILE",
    "DOCKER_CONFIG",
  ];
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (step < 3) {
      setStep(step + 1);
      return;
    }
    setBusy(true);
    try {
      const profile = await request<DeploymentCredential>(
        connection,
        `/deployment-credentials/${id}`,
        {
          method: "PUT",
          body: JSON.stringify({
            values: Object.fromEntries(
              Object.entries(values).filter(([, value]) => Boolean(value)),
            ),
            clear_keys: clear,
          }),
        },
      );
      setValues({});
      setClear([]);
      onSaved(profile);
      onClose();
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Could not save credential profile.",
      );
    } finally {
      setBusy(false);
    }
  }
  const controls = (items: string[]) =>
    items.map((key) => (
      <div className="field" key={key}>
        <label htmlFor={`credential-${key}`}>{key}</label>
        <input
          id={`credential-${key}`}
          type="password"
          autoComplete="new-password"
          maxLength={32000}
          disabled={clear.includes(key)}
          value={values[key] ?? ""}
          onChange={(event) =>
            setValues((current) => ({ ...current, [key]: event.target.value }))
          }
        />
        <small>
          {configured.includes(key)
            ? "Configured. Blank retains the existing value."
            : "Not configured."}
          {key.endsWith("_FILE") ||
          key === "GOOGLE_APPLICATION_CREDENTIALS" ||
          key === "DOCKER_CONFIG"
            ? " Supply an absolute path on the factory server."
            : ""}
        </small>
        {configured.includes(key) && (
          <label className="checkbox-line">
            <input
              type="checkbox"
              checked={clear.includes(key)}
              onChange={(event) =>
                setClear((current) =>
                  event.target.checked
                    ? [...current, key]
                    : current.filter((value) => value !== key),
                )
              }
            />
            Clear this stored key
          </label>
        )}
      </div>
    ));
  return (
    <dialog
      ref={dialog}
      className="feature-dialog"
      aria-label="Deployment credential profile"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <button
        className="quiet dialog-close"
        onClick={onClose}
        disabled={busy}
        aria-label="Close credential profile"
      >
        Close
      </button>
      <Wizard
        locked={busy}
        steps={["Profile", "Account", "SSH", "Review"]}
        active={step}
        onStep={setStep}
        title={
          [
            "Choose a private profile",
            "Configure provider credentials",
            "Configure SSH and registry access",
            "Review write-only credentials",
          ][step]
        }
        description={
          [
            "Choose a profile name or update an existing profile. Values are never returned to the browser.",
            "Only supported provider keys are accepted. Leave existing values blank to preserve them.",
            "Use server-side agent sockets and file paths. The factory freezes selected credential files when deploying.",
            "Saving private configuration does not verify account permissions or deploy software.",
          ][step]
        }
      >
        <SetupIllustration kind="cloud" step={step} />
        <form onSubmit={(event) => void submit(event)}>
          {step === 0 && (
            <>
              <div className="field">
                <label htmlFor="credential-profile-id">
                  Credential profile ID
                </label>
                <input
                  id="credential-profile-id"
                  required
                  pattern="[a-z][a-z0-9-]{0,63}"
                  list="credential-profiles"
                  value={id}
                  onChange={(event) => {
                    setId(event.target.value);
                    setValues({});
                    setClear([]);
                  }}
                />
                <datalist id="credential-profiles">
                  {profiles.map((profile) => (
                    <option key={profile.id} value={profile.id} />
                  ))}
                </datalist>
              </div>
              <p>
                {configured.length
                  ? `Configured keys: ${configured.join(", ")}`
                  : "No keys recorded for this profile."}
              </p>
            </>
          )}
          {step === 1 &&
            (accountKeys.length ? (
              controls(accountKeys)
            ) : (
              <p>
                This host recipe has no cloud account credentials. Configure SSH
                access next.
              </p>
            ))}
          {step === 2 && controls(hostKeys)}
          {step === 3 && (
            <>
              <p>
                Profile: <strong>{id}</strong>
              </p>
              <p>
                Keys being updated:{" "}
                {Object.keys(values)
                  .filter((key) => values[key])
                  .join(", ") || "None"}
              </p>
              <p>Keys being cleared: {clear.join(", ") || "None"}</p>
              <p>
                Other stored keys are retained. Secret values are sent only to
                the authenticated factory and stored privately; account
                readiness requires actual deployment evidence.
              </p>
            </>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className="wizard-actions">
            {step > 0 && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => setStep(step - 1)}
              >
                Back
              </button>
            )}
            <button disabled={busy}>
              {busy
                ? "Saving…"
                : step === 3
                  ? "Save private profile"
                  : "Continue"}
            </button>
          </div>
        </form>
      </Wizard>
    </dialog>
  );
}
