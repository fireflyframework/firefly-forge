/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useLayoutEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, ExternalLink, ShieldCheck } from "lucide-react";
import { Wizard } from "./Wizard";
import { stateNames, type ToolConnector } from "./ExternalToolsSettings";

type Requirement = { title: string; detail: string };
type Setup = {
  purpose: string;
  account: Requirement[];
  factory: Requirement[];
  source: string;
  guide: string;
};
const setups: Record<string, Setup> = {
  figma: {
    purpose:
      "Read authorized design context after vendor access and a factory adapter are approved.",
    account: [
      {
        title: "Allowed client",
        detail:
          "The official remote service restricts custom clients to its allowed catalog or waitlist. A provider account alone does not admit this factory.",
      },
      {
        title: "Authorized OAuth",
        detail:
          "Use an authorized account and OAuth connection for the intended files. This template does not start OAuth or accept a token.",
      },
      {
        title: "Project file scope",
        detail:
          "Identify the design files this product may read. A future reviewed adapter must enforce that scope.",
      },
    ],
    factory: [
      {
        title: "Admitted adapter",
        detail:
          "A Figma adapter is not available in this factory. Allowed-client approval does not install one.",
      },
      {
        title: "Exact schema review",
        detail:
          "Discovery and tool-schema review are unavailable for this template. No remote request can be sent from this view.",
      },
      {
        title: "Project grant",
        detail:
          "No Figma project grant can be created until an adapter and its scope enforcement are admitted.",
      },
    ],
    source:
      "https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/",
    guide:
      "Vendor approval, account authorization and factory admission are separate requirements.",
  },
  sentry: {
    purpose:
      "Read project-scoped diagnostic context through a reviewed adapter, with inference and issue enrichment excluded.",
    account: [
      {
        title: "Project-scoped account",
        detail:
          "Identify the organization and project whose diagnostic data the adapter may read. Use only the access that work requires.",
      },
      {
        title: "Upstream authentication",
        detail:
          "The upstream toolkit describes a Sentry-Bearer token header. This factory does not yet admit that credential or adapter.",
      },
      {
        title: "Inspect is not sufficient",
        detail:
          "An upstream skills=inspect selection can still include issue enrichment. It does not prove the factory's read-only scope.",
      },
    ],
    factory: [
      {
        title: "Scoped adapter",
        detail:
          "The required project-scoped adapter is absent. This template cannot connect to a Sentry account.",
      },
      {
        title: "Tool admission",
        detail:
          "Inference and issue enrichment must remain excluded by the reviewed adapter. Remote descriptions cannot grant those capabilities.",
      },
      {
        title: "Review and grant",
        detail:
          "Discovery, exact schema review and project grants remain unavailable until that adapter is admitted.",
      },
    ],
    source: "https://github.com/getsentry/toolkit",
    guide:
      "An upstream skill name is not a substitute for the factory's enforced tool and project restrictions.",
  },
  grafana: {
    purpose:
      "Read observability data from an approved managed HTTPS service with an enforced read-only scope.",
    account: [
      {
        title: "Managed service",
        detail:
          "Identify an approved managed HTTPS deployment. This factory does not accept an arbitrary server URL or launch a local stdio service.",
      },
      {
        title: "Read-only account",
        detail:
          "Prepare a least-privilege service account and server read-only flags. Provider access does not itself establish project isolation.",
      },
      {
        title: "Project data scope",
        detail:
          "Define which dashboards and data sources belong to this product. The reviewed adapter must enforce that boundary.",
      },
    ],
    factory: [
      {
        title: "Approved endpoint",
        detail: "No managed Grafana deployment is admitted for this template.",
      },
      {
        title: "Scoped adapter",
        detail:
          "The reviewed project-scoped, read-only adapter is absent. There is no credential form for this connection.",
      },
      {
        title: "Review and grant",
        detail:
          "Discovery, exact schema review and project grants remain unavailable until endpoint and adapter requirements are met.",
      },
    ],
    source: "https://github.com/grafana/mcp-grafana",
    guide:
      "A managed endpoint, server read-only controls and a project-scoped adapter are all required.",
  },
  playwright: {
    purpose:
      "Use the factory's native isolated browser tools for a declared project origin. An external Playwright stdio launcher is not admitted.",
    account: [
      {
        title: "Native runner profile",
        detail:
          "No provider account is needed for the native BrowserRunner. Configure the registered repository's Docker runner instead.",
      },
      {
        title: "Pinned browser image",
        detail:
          "Prepare an image pinned by SHA-256, with matching Playwright package and browser binaries, and an explicit Docker context.",
      },
      {
        title: "Container-local project",
        detail:
          "Declare the server startup arguments and a literal container-loopback origin on port 1024 or higher. Arbitrary websites are outside this runner's scope.",
      },
    ],
    factory: [
      {
        title: "Repository browser settings",
        detail:
          "Use the native runner setup guide to configure the repository's sandbox.browser profile. This checklist does not save runner settings.",
      },
      {
        title: "Execution roles",
        detail:
          "Grant browser_check or browser_journey to the applicable native agentic or Bridge role in Agents & skills.",
      },
      {
        title: "Actual worker evidence",
        detail:
          "Configured settings or cached image presence do not prove that a project server works. Inspect the worker's bounded browser result and screenshot.",
      },
    ],
    source: "https://github.com/microsoft/playwright-mcp",
    guide:
      "Native browser tools use a fresh context, network isolation and the declared project origin. They do not use an external MCP launcher.",
  },
};
export function ExternalToolPrerequisites({
  connector,
  onAgents,
}: {
  connector: ToolConnector;
  onAgents: () => void;
}) {
  const [step, setStep] = useState(0);
  const view = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const owner = view.current?.closest("main");
    if (owner) owner.scrollTop = 0;
  }, [step]);
  const setup = setups[connector.id];
  if (!setup) return null;
  const native = connector.id === "playwright";
  const requirements = step === 1 ? setup.account : setup.factory;
  const description =
    step === 0
      ? setup.purpose
      : step === 3
        ? "This view reports the saved catalog state. Following setup steps does not enable the template."
        : "These are setup requirements, not verified account or runtime receipts.";
  return (
    <div ref={view} className="external-prerequisite-frame">
      <Wizard
        className="external-tools-wizard external-prerequisites"
        progressMode="visited"
        steps={[
          "Requirements",
          native ? "Runner profile" : "Account & scope",
          "Factory setup",
          "Availability",
        ]}
        active={step}
        onStep={setStep}
        title={
          [
            `${connector.name}: required setup`,
            native
              ? "Prepare the native browser profile"
              : `Check the ${connector.name} account and scope`,
            "Review the remaining factory setup",
            `${connector.name}: current availability`,
          ][step]
        }
        description={description}
        guide={
          <>
            <ShieldCheck size={28} />
            <h3>
              {native
                ? "Native tools are separate"
                : "Admission remains required"}
            </h3>
            <p>{setup.guide}</p>
            <p className="external-hint">
              Catalog state: {stateNames[connector.readiness]}. No remote
              connection or tool request is sent by this guide.
            </p>
          </>
        }
      >
        <p className="external-short-context">{description}</p>
        {step === 0 || step === 3 ? (
          <>
            <dl className="wizard-summary">
              <div>
                <dt>Catalog state</dt>
                <dd>{stateNames[connector.readiness]}</dd>
              </div>
              <div>
                <dt>Required next</dt>
                <dd>
                  {connector.prerequisite ??
                    "A reviewed adapter and project-scoped grant are required."}
                </dd>
              </div>
              <div>
                <dt>{native ? "External template" : "Factory connection"}</dt>
                <dd>Unavailable: this template has no admitted tools.</dd>
              </div>
              <div>
                <dt>
                  {native ? "Native runner proof" : "Account verification"}
                </dt>
                <dd>Not checked by this guide.</dd>
              </div>
            </dl>
            {step === 3 && (
              <div className="external-setup-links">
                <button className="secondary" onClick={onAgents}>
                  <ShieldCheck size={16} />
                  Agents & skills
                </button>
                <a
                  className="secondary"
                  href={`https://fireflyframework.github.io/firefly-forge/${native ? "agent-tools/#configure-real-browser-checks" : `tooling-prerequisites/#${connector.id}`}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink size={16} />
                  {native
                    ? "Native runner setup guide"
                    : `${connector.name} setup guide`}
                </a>
              </div>
            )}
          </>
        ) : (
          <div className="external-requirements">
            {requirements.map((requirement) => (
              <article key={requirement.title}>
                <h3>{requirement.title}</h3>
                <p>{requirement.detail}</p>
                <span className="badge">Setup requirement</span>
              </article>
            ))}
            <a
              href={setup.source}
              target="_blank"
              rel="noopener noreferrer"
              className="text-button"
            >
              <ExternalLink size={16} />
              Official {connector.name} setup reference
            </a>
            {native && step === 2 && (
              <a
                className="text-button"
                href="https://fireflyframework.github.io/firefly-forge/agent-tools/#configure-real-browser-checks"
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink size={16} />
                Native runner setup guide
              </a>
            )}
          </div>
        )}
        <div className="wizard-actions">
          <button
            className="secondary"
            disabled={step === 0}
            onClick={() => setStep(step - 1)}
          >
            <ArrowLeft size={16} />
            Back
          </button>
          {step < 3 ? (
            <button className="primary" onClick={() => setStep(step + 1)}>
              Continue
              <ArrowRight size={16} />
            </button>
          ) : (
            <button className="secondary" disabled>
              {native
                ? "External template unavailable"
                : "Connection unavailable"}
            </button>
          )}
        </div>
      </Wizard>
    </div>
  );
}
