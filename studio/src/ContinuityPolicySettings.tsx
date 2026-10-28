/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import {
  continuityPolicy,
  continuityPolicyView,
  pendingPolicyKey,
  readPendingPolicy,
  sameContinuityPolicy,
  type ContinuityPolicyView,
  type PendingPolicy,
  type PolicyContext,
} from "./ContinuityPolicyData";
import "./ContinuityPolicySettings.css";
type Props = { connection: Connection; session: IdentitySession | null };
export function ContinuityPolicySettings({ connection, session }: Props) {
  if (!session) return null;
  const scope = JSON.stringify([
    connection.base,
    connection.token,
    session.user.id,
    session.user.authentication_method,
    session.global_admin,
  ]);
  return <Settings key={scope} connection={connection} session={session} />;
}
function Settings({
  connection,
  session,
}: {
  connection: Connection;
  session: IdentitySession;
}) {
  const context: PolicyContext = {
    base: new URL(
      normalizeBase(connection.base),
      typeof window === "undefined"
        ? "https://localhost"
        : window.location.href,
    ).href,
    actor: session.user.id,
    authentication: session.user.authentication_method,
  };
  const [saved] = useState(() => {
    try {
      return {
        pending: readPendingPolicy(
          localStorage.getItem(pendingPolicyKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { pending: null, blocked: true };
    }
  });
  const [pending, setPending] = useState<PendingPolicy | null>(saved.pending),
    [blocked, setBlocked] = useState(saved.blocked);
  const [current, setCurrent] = useState<ContinuityPolicyView | null>(null),
    [enabled, setEnabled] = useState(false),
    [checkpoint, setCheckpoint] = useState(false),
    [seconds, setSeconds] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const live = useRef(true),
    locked = useRef(false),
    reading = useRef<AbortController | null>(null),
    result = useRef<HTMLParagraphElement | null>(null);
  const allowed =
    session.global_admin &&
    session.user.authentication_method !== "scm_mapping";
  const path = "/invalid-read-continuity/policy";
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      reading.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (notice) result.current?.focus();
  }, [notice]);
  function apply(value: ContinuityPolicyView) {
    setCurrent(value);
    setEnabled(value.settings.enabled);
    setCheckpoint(value.settings.allow_checkpoint_continuation);
    setSeconds(String(value.settings.max_window_seconds));
  }
  function hide(cause: unknown) {
    if (!live.current) return;
    setCurrent(null);
    setNotice("");
    setError(
      cause instanceof ApiError && [401, 403].includes(cause.status)
        ? "Current administrator access could not be verified. Private settings are hidden."
        : "Recovery settings could not be verified. Read the saved settings before making a change.",
    );
  }
  function clearPending(expected: PendingPolicy) {
    const key = pendingPolicyKey(context),
      stored = readPendingPolicy(localStorage.getItem(key), context);
    if (!stored || !sameContinuityPolicy(stored.settings, expected.settings))
      throw Error("Saved settings identity changed");
    localStorage.removeItem(key);
    setPending(null);
  }
  async function load() {
    if (!allowed || blocked || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const controller = new AbortController();
    reading.current = controller;
    try {
      const value = continuityPolicyView(
        await request<unknown>(connection, path, { signal: controller.signal }),
      );
      if (!live.current || controller.signal.aborted) return;
      if (pending) {
        if (!sameContinuityPolicy(value.settings, pending.settings))
          throw Error("Saved settings differ");
        clearPending(pending);
        setNotice(
          "These settings are currently saved. No save request was repeated.",
        );
      }
      apply(value);
    } catch (cause) {
      if (!controller.signal.aborted) hide(cause);
    } finally {
      locked.current = false;
      if (live.current) setBusy(false);
    }
  }
  const valid =
    /^\d+$/.test(seconds) &&
    Number.isSafeInteger(Number(seconds)) &&
    Number(seconds) >= 1 &&
    Number(seconds) <= 7200;
  const desired =
    current && valid
      ? continuityPolicy({
          ...current.settings,
          enabled,
          allow_checkpoint_continuation: checkpoint,
          max_window_seconds: Number(seconds),
        })
      : null;
  const changed =
    !!current && !!desired && !sameContinuityPolicy(current.settings, desired);
  async function save() {
    if (
      !allowed ||
      blocked ||
      locked.current ||
      pending ||
      !current ||
      !desired ||
      !changed
    )
      return;
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const previous = current,
      next: PendingPolicy = { version: 1, context, settings: desired };
    const controller = new AbortController();
    reading.current = controller;
    try {
      if (!navigator.locks) throw Error("Exclusive browser lock unavailable");
      await navigator.locks.request(
        pendingPolicyKey(context),
        { mode: "exclusive" },
        async () => {
          const retained = readPendingPolicy(
            localStorage.getItem(pendingPolicyKey(context)),
            context,
          );
          if (retained) {
            if (live.current) setPending(retained);
            throw Error("An earlier save remains uncertain");
          }
          const fresh = continuityPolicyView(
            await request<unknown>(connection, path, {
              signal: controller.signal,
            }),
          );
          if (!live.current || controller.signal.aborted) return;
          if (fresh.digest !== previous.digest) {
            apply(fresh);
            setError(
              "Settings changed in another session. Review the latest values before saving.",
            );
            return;
          }
          const raw = JSON.stringify(next);
          readPendingPolicy(raw, context);
          localStorage.setItem(pendingPolicyKey(context), raw);
          if (localStorage.getItem(pendingPolicyKey(context)) !== raw)
            throw Error("Saved settings storage unavailable");
          setPending(next);
          const reply = continuityPolicyView(
            await request<unknown>(connection, path, {
              method: "PUT",
              body: JSON.stringify(next.settings),
            }),
          );
          if (!live.current) return;
          if (!sameContinuityPolicy(reply.settings, next.settings))
            throw Error("Unconfirmed saved settings");
          clearPending(next);
          apply(reply);
          setNotice(
            "Settings saved. Each continuation still needs its own review.",
          );
        },
      );
    } catch (cause) {
      if (live.current && !controller.signal.aborted) {
        try {
          const retained = readPendingPolicy(
            localStorage.getItem(pendingPolicyKey(context)),
            context,
          );
          if (retained) setPending(retained);
        } catch {
          setBlocked(true);
        }
        if (cause instanceof ApiError && [401, 403].includes(cause.status))
          hide(cause);
        else
          setError(
            "The settings save is not confirmed. Check saved settings before making another change.",
          );
      }
    } finally {
      locked.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <section
      className="continuity-policy-settings"
      aria-label="Saved-work recovery settings"
    >
      <h3>Saved-work recovery</h3>
      <p>
        Let an administrator review a paused task and continue from its saved
        response. Each continuation keeps completed work, usage and limits.
      </p>
      {!allowed ? (
        <p>A factory administrator is required to configure recovery.</p>
      ) : blocked ? (
        <p role="alert">
          Saved configuration storage cannot be verified in this browser.
          Changes are held.
        </p>
      ) : (
        <>
          <div className="card-actions">
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => void load()}
            >
              {busy
                ? "Checking…"
                : pending
                  ? "Check saved settings"
                  : current
                    ? "Refresh saved settings"
                    : "Review continuation settings"}
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
          {notice && (
            <p role="status" tabIndex={-1} ref={result}>
              {notice}
            </p>
          )}
          {pending && (
            <p>
              The saved change is awaiting confirmation. Read its current
              settings before making another change.
            </p>
          )}
          {current && !pending && (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void save();
              }}
            >
              <div className="continuity-policy-fields">
                <label className="check-choice">
                  <input
                    type="checkbox"
                    checked={enabled}
                    aria-label="Enable saved-work recovery"
                    aria-describedby="continuity-policy-enabled-help"
                    onChange={(event) => setEnabled(event.target.checked)}
                  />
                  <span>
                    Enable saved-work recovery
                    <small id="continuity-policy-enabled-help">
                      This applies to existing recovery options too.
                    </small>
                  </span>
                </label>
                <label className="check-choice">
                  <input
                    type="checkbox"
                    checked={checkpoint}
                    aria-label="Allow reviewed checkpoint continuations"
                    aria-describedby="continuity-policy-checkpoint-help"
                    onChange={(event) => setCheckpoint(event.target.checked)}
                  />
                  <span>
                    Allow reviewed checkpoint continuations
                    <small id="continuity-policy-checkpoint-help">
                      Off by default. Every task still requires a separate
                      review.
                    </small>
                  </span>
                </label>
                <div className="field">
                  <label htmlFor="continuity-policy-window">
                    Maximum time window (seconds)
                  </label>
                  <input
                    id="continuity-policy-window"
                    type="number"
                    min={1}
                    max={7200}
                    step={1}
                    value={seconds}
                    onChange={(event) => setSeconds(event.target.value)}
                    aria-describedby="continuity-policy-window-help"
                  />
                  <small id="continuity-policy-window-help">
                    Choose 1 to 7,200 seconds. A run may have a shorter
                    permitted window.
                  </small>
                </div>
              </div>
              {checkpoint && !enabled && (
                <p role="status">
                  Checkpoint continuation is selected, but saved-work recovery
                  is disabled.
                </p>
              )}
              <p>
                Other recovery options and queue limits keep their saved values.
              </p>
              <div className="card-actions">
                <button
                  type="submit"
                  className="primary"
                  disabled={busy || !valid || !changed}
                >
                  Save continuation settings
                </button>
              </div>
              <details>
                <summary>Advanced record</summary>
                <pre>{JSON.stringify(current, null, 2)}</pre>
              </details>
            </form>
          )}
        </>
      )}
    </section>
  );
}
