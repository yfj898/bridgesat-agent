# Accessibility evaluation

Covers EVALUATION_SPEC section 9. Items marked 'manual check required'
need a human usability pass before the competition demo.

| Criterion | Status | Evidence |
|---|---|---|
| WCAG 2.1 AA color contrast target | manual check required | Design target; manual check required before demo. |
| All core flows keyboard-operable | manual check required | Implemented; manual keyboard walkthrough required. |
| Visible focus indicator | verified-by-inspection | styles.css :focus-visible rules (web/styles.css:81-84). |
| Touch targets >= 44x44 CSS px | verified-by-inspection | Buttons min-height 48px (web/styles.css:65). |
| Form controls have accessible names | verified-by-inspection | aria-labelledby on cards (web/index.html:21,36). |
| Progress and error states announced to AT | verified-by-inspection | role=status on network/sync/state (web/index.html:17-18,39). |
| No required information conveyed by color alone | verified-by-inspection | Statuses carry text labels (web/index.html). |
| 200% text zoom does not block core flow | manual check required | rem/clamp sizing (web/styles.css); manual check required. |
| Reduced-motion preference respected | verified-by-inspection | No motion-critical animation in the PWA; confirm at demo. |
| Mathematical content has accessible text representation | verified-by-inspection | Math rendered as text expressions; confirm with screen reader. |
| Offline and sync state has text labels | verified-by-inspection | network-status and sync-status role=status elements. |
