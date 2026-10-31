/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useId, useRef } from "react";
import { Search, UserRound } from "lucide-react";
import "./RegisteredPersonSelector.css";

export type RegisteredPerson = {
  id: string;
  display_name: string | null;
  email: string | null;
  contact_email?: string | null;
  enabled: boolean;
};

const name = (person: RegisteredPerson) =>
  person.display_name ||
  person.contact_email ||
  person.email ||
  "Registered account";

function Identity({ person }: { person: RegisteredPerson }) {
  const primary = name(person);
  const email = person.contact_email || person.email;
  const initials = person.display_name
    ?.trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("");
  return (
    <>
      <span className="registered-person-avatar" aria-hidden="true">
        {initials || <UserRound size={16} />}
      </span>
      <span className="registered-person-identity">
        <strong>{primary}</strong>
        {email && email !== primary && <span>{email}</span>}
      </span>
    </>
  );
}

export function RegisteredPersonSelector({
  query,
  onQuery,
  onSearch,
  matches,
  value,
  onSelect,
  onChange,
  disabled,
  searching,
  complete,
  error,
}: {
  query: string;
  onQuery: (value: string) => void;
  onSearch: () => void;
  matches: RegisteredPerson[];
  value: RegisteredPerson | null;
  onSelect: (person: RegisteredPerson) => void;
  onChange: () => void;
  disabled: boolean;
  searching: boolean;
  complete: boolean;
  error: string;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const change = useRef<HTMLButtonElement>(null);
  const selectedId = value?.id;
  const hint = query.trim().length < 3;
  const previous = useRef(selectedId);
  useEffect(() => {
    if (selectedId !== previous.current) {
      if (selectedId) change.current?.focus({ preventScroll: true });
      else input.current?.focus();
    }
    previous.current = selectedId;
  }, [selectedId]);
  return (
    <div className="registered-person-selector">
      {value ? (
        <div
          className="registered-person-selected"
          role="group"
          aria-label="Selected registered person"
          aria-live="polite"
        >
          <div className="registered-person-selected-heading">
            <span>Selected person</span>
            <button
              ref={change}
              type="button"
              className="quiet"
              disabled={disabled}
              onClick={onChange}
            >
              Change
            </button>
          </div>
          <div className="registered-person-selected-identity">
            <Identity person={value} />
          </div>
        </div>
      ) : (
        <>
          <label htmlFor={id}>Find a registered person</label>
          <div className="registered-person-search">
            <div className="registered-person-query">
              <Search size={16} aria-hidden="true" />
              <input
                ref={input}
                id={id}
                type="search"
                value={query}
                placeholder="Name or email"
                maxLength={120}
                disabled={disabled}
                aria-describedby={
                  [hint ? `${id}-hint` : "", error ? `${id}-error` : ""]
                    .filter(Boolean)
                    .join(" ") || undefined
                }
                aria-invalid={Boolean(error)}
                onChange={(event) => onQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    if (!disabled) onSearch();
                  }
                }}
              />
            </div>
            <button
              type="button"
              className="secondary"
              disabled={disabled || query.trim().length < 3}
              onClick={onSearch}
            >
              Search
            </button>
          </div>
          {hint && <small id={`${id}-hint`}>Use at least 3 characters.</small>}
          {error ? (
            <p
              id={`${id}-error`}
              role="alert"
              className="registered-person-status"
            >
              {error}
            </p>
          ) : searching ? (
            <p role="status" className="registered-person-status">
              Searching registered people…
            </p>
          ) : complete && !matches.length ? (
            <p role="status" className="registered-person-status">
              No matching people. Try another name or email.
            </p>
          ) : null}
          {matches.length > 0 && (
            <fieldset className="registered-person-results" disabled={disabled}>
              <legend>Matching registered people</legend>
              {matches.map((person) => (
                <label className="registered-person-option" key={person.id}>
                  <input
                    type="radio"
                    name={`${id}-result`}
                    value={person.id}
                    checked={selectedId === person.id}
                    disabled={!person.enabled}
                    onChange={() => onSelect(person)}
                  />
                  <Identity person={person} />
                </label>
              ))}
            </fieldset>
          )}
        </>
      )}
    </div>
  );
}
