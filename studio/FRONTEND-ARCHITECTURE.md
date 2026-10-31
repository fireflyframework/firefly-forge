# Studio frontend foundation

Studio is an operations interface. Its shared foundation preserves Firefly's current Manrope typography, semantic theme variables, native form controls and product-specific layouts. It does not introduce another UI library or replace the application shell.

## Component responsibilities

| Layer | Implementation | Responsibility |
| --- | --- | --- |
| Tokens | `FormFoundation.css` and existing theme variables | Reuse the current 4px spacing rhythm. Color, typography and state contrast continue to come from the product theme. |
| Atom | `StepIndicator` | Retain an ordinal in every state. A separate check indicates validated completion; a visited step never claims validation. |
| Molecule | `FormField` | Keep a native control and its label together. Link direct native controls to their hint while preserving existing descriptions, input types and browser behavior. |
| Molecule | `ProgressHeader` | Give Wizard titles, descriptions and contextual actions one consistent structure. Product layouts may retain their existing heading arrangement. |
| Molecule | `ScrollRegion` | Own bounded field scrolling, keyboard entry and a visible remaining-content cue. Keep actions outside that region. |
| Molecule | `PageScrollStatus` | Describe the existing main page scroller in a reserved row above the footer. Provide an explicit return to the form or recovery section start after scrolling. |
| Layout | `.form-stack`, `.form-grid`, `.form-divider` | Compose vertical fields, progress and scrolling regions, and action separators with inherited spacing and theme tokens. |
| Composite | `Wizard` | Compose progress, heading, fields, guide and actions. Retain navigation validation, visited-step semantics and the existing container measurement. |
| Product surfaces | Product definition, provider configuration and recovery | Supply domain labels, native control state, validation, authority and submission behavior. The foundation does not make those decisions. |

## Layout contracts

The shared `--wizard-inline-inset` controls the leading edge of progress indicators, headings, field regions and action rows. The default follows the incumbent 28px desktop and 16px narrow layout. The product editor retains its 20px desktop and 12px narrow inset. Do not compensate with independent button padding or a second hardcoded left offset.

A bounded Wizard uses the available space measured from its actual container. Its form grid reserves an action row; only the field region scrolls. There is no guessed initial 640px frame height. Native inputs remain keyboard reachable, and overflow makes the named field region focusable.

On short or narrow layouts that use page flow, the fields expand naturally and the existing `main` element owns scrolling. Actions remain in normal flow rather than covering fields through a fixed or sticky overlay. Product-specific parent layouts keep their existing breakpoints. Avoid adding another overflow container around these forms.

The main page's scroll status occupies a real grid row above the footer. It never overlays form content or moves the footer into the content scroller. When a title has naturally scrolled away, **Back to section start** returns the form's progress and heading to view using actual element bounds. It creates no execution or submission action.

Step changes focus the new heading without forcing an outer page scroll to a small progress indicator. The current step is still brought into horizontal view when needed. Field focus uses the browser's native scrolling rather than a product-specific handler that moves an unrelated ancestor.

## Boundaries and verification

The foundation is used by the shared Wizard and real product/provider fields. It does not change backend contracts, request identities, authorization, recovery receipts or publication policy. The claimed-result card continues to use the original request and GET-only readback after an uncertain submission.

`Wizard.test.tsx` covers numeric and completion semantics; `FormPrimitives.test.tsx` covers native labels, hints and region structure. `form-foundation.spec.ts` exercises real product, provider and recovery surfaces at desktop, 390px and 320px widths, including short heights and both themes. It records actual geometry and screenshots. Canonical documentation captures retain their existing source, helper, producer, version and image hash checks.
