/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type SetStateAction,
} from "react";
import {
  normalizeBase,
  type AIProposalProvenance,
  type Connection,
  type CustomerProductDefinition,
  type ProductBrand,
  type ProductSnapshot,
} from "./api";

let sequence = 0;
const cut = (value: ProductSnapshot | null) =>
  value ? `${value.revision}:${value.digest}` : "missing";

export class ProductDraftSession {
  readonly id = ++sequence;
  readonly repository: string;
  private fields = new Map<string, unknown>();
  private listeners = new Set<() => void>();
  private closed = false;
  observed = false;
  base: ProductSnapshot | null = null;
  brand: ProductBrand | null = null;
  private latest: ProductSnapshot | null = null;
  editVersion = 0;
  constructor(repository: string) {
    this.repository = repository;
  }
  get active() {
    return !this.closed;
  }
  get dirty() {
    return Boolean(this.fields.get("dirty"));
  }
  get stale() {
    return this.observed && cut(this.base) !== cut(this.latest);
  }
  readonly subscribe = (listener: () => void) => {
    if (!this.closed) this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private changed() {
    for (const listener of this.listeners) listener();
  }
  read<T>(name: string, initial: T): T {
    if (this.closed) return initial;
    if (!this.fields.has(name)) this.fields.set(name, structuredClone(initial));
    return this.fields.get(name) as T;
  }
  write<T>(name: string, value: T, editable = false) {
    if (this.closed || Object.is(this.fields.get(name), value)) return;
    this.fields.set(name, structuredClone(value));
    if (editable) {
      this.fields.set("dirty", true);
      this.fields.set("savedDraft", false);
      this.fields.set("saved", false);
      this.editVersion++;
    }
    this.changed();
  }
  observe(snapshot: ProductSnapshot | null, brand: ProductBrand | null) {
    if (this.closed) return;
    if (
      (snapshot && snapshot.repository_id !== this.repository) ||
      (brand && brand.repository_id !== this.repository)
    )
      throw new Error("The product response belongs to another repository.");
    this.latest = structuredClone(snapshot);
    if (!this.observed) {
      this.base = structuredClone(snapshot);
      this.brand = structuredClone(brand);
      this.observed = true;
    }
    this.changed();
  }
  proposalSession() {
    const next = new ProductDraftSession(this.repository);
    next.observed = this.observed;
    next.base = structuredClone(this.base);
    next.latest = structuredClone(this.latest);
    next.brand = structuredClone(this.brand);
    return next;
  }
  acceptSaved(snapshot: ProductSnapshot, version: number) {
    if (this.closed) return false;
    if (snapshot.repository_id !== this.repository)
      throw new Error("The saved product belongs to another repository.");
    const changedCut = this.stale && cut(this.latest) !== cut(snapshot);
    if (!changedCut) this.latest = structuredClone(snapshot);
    if (version !== this.editVersion || changedCut) {
      this.changed();
      return false;
    }
    this.base = structuredClone(snapshot);
    this.fields.set("dirty", false);
    this.fields.set("savedDraft", true);
    this.changed();
    return true;
  }
  close() {
    this.closed = true;
    this.fields.clear();
    this.base = this.latest = this.brand = null;
    this.observed = false;
    this.listeners.clear();
  }
}

export class ProductDraftBook {
  constructor(
    readonly scope?: {
      base: string;
      user: string | undefined;
      credentialPresent: boolean;
    },
  ) {}
  readonly id = ++sequence;
  private sessions = new Map<string, ProductDraftSession>();
  private listeners = new Set<() => void>();
  private closed = false;
  get hasUnsaved() {
    return [...this.sessions.values()].some((session) => session.dirty);
  }
  readonly subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  repository(id: string) {
    if (this.closed)
      throw new Error("The previous product editing session has ended.");
    let session = this.sessions.get(id);
    if (!session) {
      session = new ProductDraftSession(id);
      this.sessions.set(id, session);
    }
    return session;
  }
  private replace(session: ProductDraftSession, next: ProductDraftSession) {
    if (this.closed || this.sessions.get(session.repository) !== session)
      return;
    session.close();
    this.sessions.set(session.repository, next);
    for (const listener of this.listeners) listener();
  }
  discard(session: ProductDraftSession) {
    this.replace(session, new ProductDraftSession(session.repository));
  }
  apply(
    session: ProductDraftSession,
    definition: CustomerProductDefinition,
    source: AIProposalProvenance,
  ) {
    const next = session.proposalSession();
    next.write(
      "draft",
      {
        ...definition,
        ai_origin: {
          conversation_id: source.conversation_id,
          turn_id: source.turn_id,
          proposal_digest: source.proposal_digest,
        },
      },
      true,
    );
    next.write("draftSource", source);
    this.replace(session, next);
  }
  dispose() {
    this.closed = true;
    for (const session of this.sessions.values()) session.close();
    this.sessions.clear();
    this.listeners.clear();
  }
}

export function useProductDraftSession(
  book: ProductDraftBook,
  repository: string,
) {
  const read = useCallback(
    () => book.repository(repository),
    [book, repository],
  );
  return useSyncExternalStore(book.subscribe, read, read);
}

export function useProductDraftField<T>(
  session: ProductDraftSession,
  name: string,
  initial: T,
  editable = false,
) {
  const read = useCallback(
    () => session.read(name, initial),
    [session, name, initial],
  );
  const value = useSyncExternalStore(session.subscribe, read, read);
  const update = useCallback(
    (action: SetStateAction<T>) => {
      const previous = session.read(name, initial);
      session.write(
        name,
        typeof action === "function"
          ? (action as (current: T) => T)(previous)
          : action,
        editable,
      );
    },
    [session, name, initial, editable],
  );
  return [value, update] as const;
}

export function warnProductDraftUnload(
  book: ProductDraftBook,
  event: { preventDefault: () => void; returnValue: unknown },
) {
  if (!book.hasUnsaved) return;
  event.preventDefault();
  event.returnValue = "";
}

export function useProductDraftBook(
  connection: Connection,
  user: string | undefined,
) {
  const base = normalizeBase(connection.base);
  const book = useMemo(
    () =>
      new ProductDraftBook({
        base,
        user,
        credentialPresent: Boolean(connection.token),
      }),
    [base, connection.token, user],
  );
  const previous = useRef(book);
  useEffect(() => {
    if (previous.current !== book) previous.current.dispose();
    previous.current = book;
  }, [book]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) =>
      warnProductDraftUnload(book, event);
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [book]);
  return book;
}
