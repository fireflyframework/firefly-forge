/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  ArrowRight,
  Bot,
  Database,
  GitBranch,
  Globe,
  GraduationCap,
  Server,
  Settings2,
  ShieldCheck,
  Users,
  Waypoints,
} from "lucide-react";
import type { IdentitySession } from "./Identity";
import "./Administration.css";

export type AdministrationPage =
  | "access"
  | "users"
  | "settings"
  | "workflow"
  | "collaboration"
  | "search"
  | "learning"
  | "analytics"
  | "runners"
  | "browser-connections"
  | "external-tools";
const factoryPages = new Set([
  "admin",
  "users",
  "settings",
  "search",
  "learning",
  "runners",
  "browser-connections",
  "external-tools",
]);
export function requiresFactoryAdministrator(
  page: string,
  session: IdentitySession | null,
): boolean {
  return (
    factoryPages.has(page) &&
    !session?.global_admin &&
    !(page === "settings" && !session)
  );
}
export function Administration({
  session,
  onNavigate,
}: {
  session: IdentitySession | null;
  onNavigate: (page: AdministrationPage) => void;
}) {
  const administrator = Boolean(session?.global_admin);
  if (!administrator)
    return (
      <section className="panel" aria-label="Factory administration">
        <h1>Factory administration</h1>
        <p>Factory administration requires an administrator account.</p>
        <p>
          Use your account menu for personal tokens, preferences and project
          access.
        </p>
      </section>
    );
  const cards = [
    {
      page: "runners" as const,
      icon: Server,
      title: "Runners & processes",
      body: "Configure repository execution and owned process waits for new runs.",
    },
    {
      page: "analytics" as const,
      icon: Database,
      title: "Structured analytics",
      body: "Inspect structured project knowledge through its saved read-only catalog scope.",
    },
    {
      page: "learning" as const,
      icon: GraduationCap,
      title: "Learning",
      body: "Choose which verified project knowledge new runs can reuse.",
    },
    {
      page: "browser-connections" as const,
      icon: Globe,
      title: "Connected accounts",
      body: "Register a client, authorize an account in your browser, and manage its connection.",
    },
    {
      page: "access" as const,
      icon: ShieldCheck,
      title: "Project access",
      body: "Choose who can work on each project and what they can do.",
    },
    {
      page: "users" as const,
      icon: Users,
      title: "People",
      body: "Add people, send invitations, and manage account access.",
    },
    {
      page: "settings" as const,
      icon: Settings2,
      title: "Connections",
      body: "Connect coding providers, repositories, and project knowledge.",
    },
    {
      page: "search" as const,
      icon: Globe,
      title: "Internet search",
      body: "Choose a search provider, store its key privately, and restrict public sources.",
    },
    {
      page: "workflow" as const,
      icon: Bot,
      title: "Agents & skills",
      body: "Choose how coding agents work and which tools they can use.",
    },
    {
      page: "external-tools" as const,
      icon: Waypoints,
      title: "External tools",
      body: "Connect tools and choose the repositories that can use them.",
    },
    {
      page: "collaboration" as const,
      icon: GitBranch,
      title: "Repository workflow",
      body: "Connect issue and pull-request activity to project work.",
    },
  ];
  const groups = [
    {
      name: "People & access",
      detail: "Manage accounts and project permissions.",
      pages: ["users", "access"],
    },
    {
      name: "Connections",
      detail: "Connect services and control how the factory reaches them.",
      pages: [
        "settings",
        "browser-connections",
        "external-tools",
        "collaboration",
        "search",
        "analytics",
      ],
    },
    {
      name: "Execution",
      detail: "Configure runners, agents and the knowledge they use.",
      pages: ["runners", "workflow", "learning"],
    },
  ];
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Administration</h1>
          <p>Manage people, connected services, and how work is performed.</p>
        </div>
      </div>
      <div className="administration-groups" aria-label="Factory settings">
        {groups.map((group) => (
          <section
            className="administration-group"
            aria-label={group.name}
            key={group.name}
          >
            <header>
              <h2>{group.name}</h2>
              <p>{group.detail}</p>
            </header>
            <div className="administration-rows">
              {group.pages
                .flatMap((page) => cards.filter((card) => card.page === page))
                .map((card) => (
                  <button
                    className="administration-row"
                    key={card.page}
                    aria-label={card.title}
                    aria-describedby={`administration-${card.page}-description`}
                    onClick={() => onNavigate(card.page)}
                  >
                    <span className="administration-icon">
                      <card.icon size={20} />
                    </span>
                    <span>
                      <strong>{card.title}</strong>
                      <small id={`administration-${card.page}-description`}>
                        {card.body}
                      </small>
                    </span>
                    <ArrowRight size={18} />
                  </button>
                ))}
            </div>
          </section>
        ))}
      </div>
    </>
  );
}
