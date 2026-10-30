/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useLayoutEffect, useRef } from "react";
import { Heart } from "lucide-react";
import "./FoundationFooter.css";

export function FoundationFooter({
  className = "",
  version,
  documentation = false,
}: {
  className?: string;
  version?: string;
  documentation?: boolean;
}) {
  const footer = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (!version) return;
    const node = footer.current;
    const shell = node?.closest<HTMLElement>(".workspace-main");
    const header = shell?.querySelector<HTMLElement>(":scope > header");
    if (!node || !shell || !header) return;
    const measure = () => {
      const top = header.getBoundingClientRect().bottom;
      const bottom = node.getBoundingClientRect().top;
      shell.style.setProperty("--shell-content-top", `${top}px`);
      shell.style.setProperty(
        "--shell-content-height",
        `${Math.max(0, bottom - top)}px`,
      );
      shell.style.setProperty(
        "--shell-footer-extent",
        `${Math.max(0, innerHeight - bottom)}px`,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    observer.observe(header);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [version]);
  return (
    <footer ref={footer} className={`foundation-footer ${className}`.trim()}>
      <span className="foundation-copyright">
        © 2026 Firefly Software Foundation
      </span>
      <span className="foundation-credits">
        <span className="foundation-made">
          Made with{" "}
          <span className="foundation-heart" role="img" aria-label="love">
            <Heart size={13} aria-hidden="true" />
          </span>
        </span>
        <a
          href="https://www.apache.org/licenses/LICENSE-2.0"
          target="_blank"
          rel="noopener noreferrer"
        >
          Apache 2.0
        </a>
        {documentation && (
          <a
            href="https://fireflyframework.github.io/firefly-forge/"
            target="_blank"
            rel="noopener noreferrer"
          >
            Documentation
          </a>
        )}
      </span>
      {version && <span>v{version}</span>}
    </footer>
  );
}
