/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Menu, X, type LucideIcon } from "lucide-react";
import "./MobileNavigation.css";
export function MobileNavigation({
  items,
  current,
  onNavigate,
  account,
}: {
  items: { id: string; label: string; icon: LucideIcon; count?: number }[];
  current: string;
  onNavigate: (id: string) => void;
  account?: { label: string; content: ReactNode; onOpen: () => void };
}) {
  const [open, setOpen] = useState(false),
    dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  useEffect(() => {
    const query = matchMedia("(max-width:900px)");
    const resize = () => {
      if (!query.matches) setOpen(false);
    };
    query.addEventListener("change", resize);
    return () => query.removeEventListener("change", resize);
  }, []);
  return (
    <div className="mobile-navigation">
      <button
        className="secondary mobile-menu-trigger"
        aria-label="Open navigation"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        <Menu size={20} />
        Menu
      </button>
      <dialog
        ref={dialog}
        className="mobile-menu-dialog"
        aria-labelledby="mobile-menu-title"
        onCancel={() => setOpen(false)}
      >
        <header>
          <div>
            <h2 id="mobile-menu-title">Workspace navigation</h2>
            <p>Choose where to work.</p>
          </div>
          <button
            className="quiet"
            aria-label="Close navigation"
            onClick={() => setOpen(false)}
          >
            <X size={20} />
          </button>
        </header>
        <nav aria-label="Main navigation">
          {items.map(({ id, label, icon: Icon, count }) => (
            <button
              key={id}
              className={current === id ? "active" : ""}
              aria-current={current === id ? "page" : undefined}
              onClick={() => {
                setOpen(false);
                onNavigate(id);
              }}
            >
              <Icon size={20} />
              <span>{label}</span>
              {count !== undefined && <small>{count}</small>}
            </button>
          ))}
        </nav>
        {account && (
          <div className="mobile-account">
            <button
              className="secondary identity-trigger"
              aria-label={account.label}
              aria-haspopup="dialog"
              onClick={() => {
                setOpen(false);
                account.onOpen();
              }}
            >
              {account.content}
            </button>
          </div>
        )}
      </dialog>
    </div>
  );
}
