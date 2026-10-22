/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useCallback, useEffect, useRef, useState } from "react";
import { MessageSquare, RefreshCw, Save } from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import {
  communicationBindings,
  communicationConfiguration,
  communicationSaved,
  type CommunicationConfiguration,
} from "./CommunicationSettingsData";
export function CommunicationSettings({
  connection,
  canConfigure,
}: {
  connection: Connection;
  canConfigure: boolean;
}) {
  const [current, setCurrent] = useState<CommunicationConfiguration | null>(
      null,
    ),
    [selected, setSelected] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const scope = useRef(connection);
  scope.current = connection;
  const read = useCallback(
    async (signal?: AbortSignal) => {
      setBusy(true);
      setError("");
      try {
        const [profiles, route] = await Promise.all([
          request<unknown>(connection, "/model-profiles", { signal }),
          request<unknown>(connection, "/task-routing", { signal }),
        ]);
        if (signal?.aborted || scope.current !== connection) return;
        const configuration = communicationConfiguration(profiles, route);
        setCurrent(configuration);
        setSelected(configuration.bindings.communication ?? "");
      } catch (cause) {
        if (!signal?.aborted && scope.current === connection) {
          setCurrent(null);
          setError(
            cause instanceof Error
              ? cause.message
              : "Communication settings could not be read.",
          );
        }
      } finally {
        if (!signal?.aborted && scope.current === connection) setBusy(false);
      }
    },
    [connection],
  );
  useEffect(() => {
    const controller = new AbortController();
    void read(controller.signal);
    return () => controller.abort();
  }, [read]);
  async function save() {
    if (!current || !canConfigure || busy) return;
    const desired = communicationBindings(current, selected);
    setBusy(true);
    setError("");
    setNotice("");
    let message = "",
      refused = false;
    try {
      const acknowledgment = await request(connection, "/task-routing", {
        method: "PUT",
        body: JSON.stringify({
          bindings: desired,
          expected_digest: current.digest,
        }),
      });
      if (scope.current !== connection) return;
      communicationSaved(acknowledgment, current, desired);
      message =
        "Communication profile saved. Future updates use this selection; earlier messages are unchanged.";
    } catch (cause) {
      if (scope.current !== connection) return;
      refused =
        cause instanceof ApiError &&
        cause.responseKind === "http" &&
        [401, 403, 409, 422].includes(cause.status);
      message = refused
        ? "The server declined this change. Refresh your access or review the current selection before saving again. No automatic retry was sent."
        : "The save response was not confirmed. Read the current selection below before deciding whether to save again. No automatic retry was sent.";
    } finally {
      if (scope.current === connection) {
        await read();
        if (scope.current === connection) {
          if (refused) setError(message);
          else setNotice(message);
        }
      }
    }
  }
  return (
    <section className="integration-card" aria-label="Project update wording">
      <h3>
        <MessageSquare size={20} aria-hidden="true" /> Project updates
      </h3>
      <p>
        Choose who writes issue and pull-request updates. AI wording uses
        recorded run facts; questions, choices and execution controls stay with
        forge.
      </p>
      <div className="field">
        <label htmlFor="communication-profile">Communication profile</label>
        <select
          id="communication-profile"
          value={selected}
          disabled={busy || !current || !canConfigure}
          onChange={(event) => setSelected(event.target.value)}
        >
          <option value="">Recorded updates · no AI wording</option>
          {current?.profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.name} ·{" "}
              {profile.provider === "bridge"
                ? "Bridge subscription"
                : "API model"}
            </option>
          ))}
        </select>
      </div>
      <p className="subtle">
        AI writes and reviews each complete update, with one correction before
        responding when needed. If the selected provider is unavailable, reaches
        its usage limit or returns an invalid message format, delivery stays
        held without a substitute message. Coding subscriptions are never
        selected implicitly.
      </p>
      {current && !current.profiles.length && (
        <p>
          No compatible communication profiles are configured. An administrator
          can add an API or Bridge model profile, then select it here.
        </p>
      )}
      {!canConfigure && (
        <p>A factory administrator can change this selection.</p>
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
          disabled={busy}
          onClick={() => void read()}
        >
          <RefreshCw size={16} /> Refresh selection
        </button>
        <button
          className="primary"
          disabled={
            busy ||
            !current ||
            !canConfigure ||
            selected === (current.bindings.communication ?? "")
          }
          onClick={() => void save()}
        >
          <Save size={16} /> Save communication profile
        </button>
      </div>
    </section>
  );
}
