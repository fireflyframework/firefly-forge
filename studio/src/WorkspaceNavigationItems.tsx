/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { Fragment } from "react";
import type { LucideIcon } from "lucide-react";
import "./WorkspaceNavigationItems.css";

export type WorkspaceNavigationItem = {
  id: string;
  label: string;
  icon: LucideIcon;
  group?: string;
  count?: number;
};

export function WorkspaceNavigationItems({
  items,
  current,
  onNavigate,
}: {
  items: WorkspaceNavigationItem[];
  current: string;
  onNavigate: (id: string) => void;
}) {
  return items.map(({ id, label, icon: Icon, group, count }, index) => (
    <Fragment key={id}>
      {group && group !== items[index - 1]?.group && (
        <h2 className="workspace-navigation-group">{group}</h2>
      )}
      <button
        className={current === id ? "active" : ""}
        aria-current={current === id ? "page" : undefined}
        onClick={() => onNavigate(id)}
      >
        <Icon size={20} aria-hidden="true" />
        <span>{label}</span>
        {count !== undefined && <small>{count}</small>}
      </button>
    </Fragment>
  ));
}
