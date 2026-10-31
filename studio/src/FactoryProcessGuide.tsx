/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  ArrowRight,
  BookOpen,
  Box,
  ClipboardList,
  ChevronDown,
  FileCheck2,
  GitBranch,
  Plug,
  Waypoints,
} from "lucide-react";
import "./FactoryProcessGuide.css";

export type FactoryProcessDestination =
  | "settings"
  | "repositories"
  | "product"
  | "projects"
  | "new"
  | "delivery"
  | "operations"
  | "collaboration";

export function FactoryProcessGuide({
  canConfigure,
  onNavigate,
}: {
  canConfigure: boolean;
  onNavigate: (page: FactoryProcessDestination) => void;
}) {
  const stages = [
    {
      title: "Prepare the workspace",
      icon: Plug,
      action: canConfigure ? "Connections" : "Repositories",
      page: canConfigure ? ("settings" as const) : ("repositories" as const),
      body: canConfigure
        ? "Connect repository access, a coding provider and knowledge. Configure runners and tools before work needs them."
        : "Check the repositories you can use. Your factory administrator manages provider, knowledge and runner connections.",
      result: "Authorized source and a ready execution environment.",
    },
    {
      title: "Define or understand the product",
      icon: BookOpen,
      action: "Product definition",
      page: "product" as const,
      body: "Agree on the customer, journeys, features and acceptance checks. For an existing product, review its source in Product projects first.",
      result:
        "A saved definition of the behavior you want, including APIs without a frontend.",
    },
    {
      title: "Plan the delivery",
      icon: ClipboardList,
      action: "Product projects",
      page: "projects" as const,
      body: "Group repositories into a product project and configure its components. Start one component run here, or request a campaign across components through the API, SDK or MCP.",
      result: "A delivery scope with component runs you can follow together.",
    },
    {
      title: "Build and review",
      icon: FileCheck2,
      action: "Start a run",
      page: "new" as const,
      body: "Build a change, maintain existing code or run its tests. Follow the work in Factory; answer agent questions and review changes and checks in run details.",
      result:
        "A source checkpoint with recorded verification and any required review.",
    },
    {
      title: "Deliver and operate",
      icon: Box,
      action: "Deployment",
      page: "delivery" as const,
      body: "Publish the reviewed change for a pull or merge request. Configure deployment separately, then follow health and maintenance in Applications.",
      result:
        "Delivery and application observations tied to the source you reviewed.",
    },
  ];
  return (
    <details className="factory-process-guide">
      <summary>
        <Waypoints size={18} aria-hidden="true" />
        <span>How work moves through forge</span>
        <ChevronDown
          className="factory-process-toggle"
          size={18}
          aria-hidden="true"
        />
      </summary>
      <div className="factory-process-body">
        <p className="factory-process-intro">
          Start with your next decision. A new product begins with its customer;
          an existing product can join at its current stage. An incremental
          feature updates the saved definition rather than starting it again.
        </p>
        <ol>
          {stages.map(({ title, icon: Icon, action, page, body, result }) => (
            <li key={page}>
              <Icon size={20} aria-hidden="true" />
              <div>
                <h2>{title}</h2>
                <p>{body}</p>
                <p className="factory-process-result">
                  <strong>Result:</strong> {result}
                </p>
              </div>
              <button
                type="button"
                className="secondary"
                onClick={() => onNavigate(page)}
              >
                {action}
                <ArrowRight size={16} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ol>
        <div className="factory-process-channel">
          <GitBranch size={20} aria-hidden="true" />
          <div>
            <h2>Work from your repository conversation</h2>
            <p>
              Connect the repository workflow to receive requests and send
              updates through GitHub, GitLab or Bitbucket. Keep decisions and
              questions with the issue or review request; use the linked run to
              inspect its execution evidence.
            </p>
          </div>
          <button
            type="button"
            className="secondary"
            onClick={() => onNavigate("collaboration")}
          >
            Repository workflow
            <ArrowRight size={16} aria-hidden="true" />
          </button>
        </div>
        <a href="https://fireflyframework.github.io/firefly-forge/factory-journeys/">
          Follow the illustrated product journey
          <ArrowRight size={16} aria-hidden="true" />
        </a>
      </div>
    </details>
  );
}
