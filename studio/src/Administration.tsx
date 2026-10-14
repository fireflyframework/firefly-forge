/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  ArrowRight,
  Bot,
  GitBranch,
  Globe,
  GraduationCap,
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
  | "browser-connections"
  | "external-tools";
export function Administration({
  session,
  onNavigate,
}: {
  session: IdentitySession | null;
  onNavigate: (page: AdministrationPage) => void;
}) {
  const administrator = Boolean(session?.global_admin);
  const owner = Boolean(
    session?.memberships.some((item) => item.permissions.includes("members")),
  );
  const cards = [
    {
      page: "learning" as const,
      icon: GraduationCap,
      title: "Learning",
      body: "Choose how new runs reuse verified project knowledge and keep retrieval bounded.",
      allowed: administrator,
      scope: "Factory administrator",
    },
    {
      page: "browser-connections" as const,
      icon: Globe,
      title: "Connected accounts",
      body: "Register a client, authorize an account in your browser, and manage its connection.",
      allowed: administrator,
      scope: "Factory administrator",
    },
    {
      page: "access" as const,
      icon: ShieldCheck,
      title: "Project access",
      body: "Choose who can work on each project and what they can do.",
      allowed: Boolean(session),
      scope: "Project owner or factory administrator",
    },
    {
      page: "users" as const,
      icon: Users,
      title: "People",
      body: "Add people, send invitations, and manage account access.",
      allowed: administrator,
      scope: "Factory administrator",
    },
    {
      page: "settings" as const,
      icon: Settings2,
      title: "Connections",
      body: "Connect coding providers, repositories, and project knowledge.",
      allowed: administrator || !session,
      scope: "Factory administrator",
    },
    {
      page: "search" as const,
      icon: Globe,
      title: "Internet search",
      body: "Choose a search provider, store its key privately, and restrict public sources.",
      allowed: administrator,
      scope: "Factory administrator",
    },
    {
      page: "workflow" as const,
      icon: Bot,
      title: "Agents & skills",
      body: "Choose how coding agents work and which tools they can use.",
      allowed: Boolean(session),
      scope: "Catalog read; project policies restricted",
    },
    {
      page: "external-tools" as const,
      icon: Waypoints,
      title: "External tools",
      body: "Connect reviewed tools and grant access to a project repository.",
      allowed: administrator,
      scope: "Factory administrator",
    },
    {
      page: "collaboration" as const,
      icon: GitBranch,
      title: "Repository workflow",
      body: "Connect issue and pull-request activity to project work.",
      allowed: administrator || owner,
      scope: "Project owner; global bootstrap restricted",
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
      <section
        className="administration-cards"
        aria-label="Administration areas"
      >
        {cards.map((card) => (
          <article key={card.page}>
            <card.icon size={24} />
            <h2>{card.title}</h2>
            <p>{card.body}</p>
            <small>{card.scope}</small>
            <button
              className="secondary"
              disabled={!card.allowed}
              onClick={() => onNavigate(card.page)}
            >
              {card.title}
              <ArrowRight size={16} />
            </button>
            {!card.allowed && (
              <span className="administration-restriction">
                {session
                  ? "Your session does not grant this permission."
                  : "Sign in to inspect your permissions."}
              </span>
            )}
          </article>
        ))}
      </section>
    </>
  );
}
