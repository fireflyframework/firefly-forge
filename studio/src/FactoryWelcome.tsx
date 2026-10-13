/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  ArrowRight,
  FolderGit2,
  Plug,
  MessageSquare,
  Layers3,
} from "lucide-react";
import "./FactoryWelcome.css";
export function FactoryWelcome({
  theme,
  onConnect,
  onProduct,
}: {
  theme: "light" | "dark";
  onConnect: () => void;
  onProduct: () => void;
}) {
  return (
    <section className="factory-welcome" aria-label="Factory onboarding">
      <div className="page-heading">
        <div>
          <h1>Connect your software factory</h1>
          <p>
            Connect a server to work with your repositories, coding providers
            and recorded runs.
          </p>
        </div>
      </div>
      <div className="factory-welcome-layout">
        <div className="factory-welcome-start">
          <h2>Start with your workspace</h2>
          <p>
            Choose your factory server and sign in. Your server manages your
            session and project access.
          </p>
          <div className="feature-actions">
            <button className="primary" onClick={onConnect}>
              <Plug size={18} />
              Connect factory
              <ArrowRight size={18} />
            </button>
            <button className="secondary" onClick={onProduct}>
              <Layers3 size={18} />
              Start a product
            </button>
          </div>
          <small>
            Starting a product first opens connection setup, then the product
            editor.
          </small>
          <ol className="factory-onboarding-steps">
            {[
              {
                icon: Plug,
                name: "Connect the server",
                detail:
                  "Choose the server address, then use its sign-in method.",
              },
              {
                icon: MessageSquare,
                name: "Choose execution",
                detail:
                  "Configure a coding provider, or run existing checks without a model.",
              },
              {
                icon: FolderGit2,
                name: "Add source and define the product",
                detail:
                  "Grant repository access or add Git source; review the customer and behavior before running work.",
              },
            ].map(({ icon: Icon, name, detail }, i) => (
              <li key={name}>
                <span className="welcome-step-number">{i + 1}</span>
                <Icon size={20} />
                <div>
                  <strong>{name}</strong>
                  <p>{detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
        <figure
          className="factory-welcome-preview"
          aria-label="Factory preview"
          data-theme={theme}
        >
          <img
            src={`/assets/forge-factory-${theme}.svg`}
            alt="Preview of the Firefly software factory"
          />
          <figcaption>
            Factory preview · no live project or run data is connected.
          </figcaption>
        </figure>
      </div>
    </section>
  );
}
