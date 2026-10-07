<!-- run full | model claude-sonnet-5 | prompt memo_v4@083aa894 | automatic check passed: True | human review: APPROVED by Luiza Leite Teixeira (author) on 2026-10-07 (see docs/memo_review.md) -->

## Recommendation

**Spotify should spend next quarter's product effort making free-tier Premium-only restrictions clearer and testing whether some core playback controls can be adjusted or trialed for free users.** This is the billing_support area's top issue, ISS-BILLING-PREMIUM_ONLY_CONTROLS, with 59,334 complaints [C001] and a severity-weighted priority score of 175,269 [C002][C004] - the single highest-priority issue in the entire pack, ahead of all usability, playback, and access issues individually. It also carries 11,677 cancellation-intent mentions [ISS-BILLING-PREMIUM_ONLY_CONTROLS], the largest expressed-intent volume of any single issue, and the monthly trend shows it escalating sharply in the most recent data.

Importantly, per the pack's `issue_definitions`, this issue is about "song choice, skips, rewind, order, lyrics etc. explicitly locked behind Premium" - a **free-tier packaging/pricing decision**, classified under billing only by the course's shared label rule. It is not a payment-processing or customer-support failure, and the fix is product work on the free-tier experience, not billing operations.

## Evidence

- Billing_support area totals: 71,289 complaints, severity_sum 206,126, 12,890 cancellation mentions (area table).
- Top issue alone: 59,334 complaints [C001], severity_sum 175,269 [C002], mean_severity 2.953939 [C003].
- Representative reviews describe being unable to choose or skip songs, e.g. `2d1b5fa6-5d80-4616-9b65-f6e7738e9edf`: "I can't actually play the songs I want to because it just plays a different song when I try." and `4f8b85b3-da73-40fa-b7e4-5841c2385e5f`: "Recent updates disabled Queue, Skips, Rewind and the ability to play the song you desire"
- Monthly trend (comparable full months, excluding partial 2022-05 and 2023-11): share held around 3.30%-4.78% from 2022-06 through 2023-05, then rose to 8.01% in 2023-06, 17.13% in 2023-09, and 29.31% in 2023-10. Review volume varies substantially by month (e.g., 85,076 reviews in 2023-07 vs 27,114 in 2023-06), so these are shares, not absolute counts.

## Alternatives considered

- **Usability**: highest complaint_count (75,546) but lower mean_severity (2.370185); its top sub-issues (ads, priority 85,570 [C012]; redesign, priority 40,196 [C020]) trail the top billing issue.
- **Playback**: highest sev4plus count (11,434) but its leading issue (stops/skips) scores only 44,320 [C016] in priority, well below billing's top issue.
- **Access**: highest mean_severity (3.839357) but smallest complaint base (12,811); top issue (login) scores 31,486 [C028].
- **General dissatisfaction** (ISS-OTHER-GENERAL) ranks second overall by priority score (147,684 [C008], 73,802 complaints [C005]), but per its definition covers "general praise, generic criticism, unrelated or unclear text" - it is not actionable as a specific product fix and is reported for context only.

## Risks and limitations

Label reliability is imperfect: a small 50-case human golden set shows topic_accuracy of 0.7, and notably 0 human-labeled billing cases versus 5 model-labeled billing cases in that tiny sample, suggesting some risk of topic misclassification for this exact label. An independent verifier on a larger 2,000-review sample found overall topic_agreement_all of 0.8475, and on the top issue specifically, 197 of 255 verified cases shared the same topic. Given the large lead in priority score (175,269 vs the next issue's 147,684), this level of disagreement is unlikely to overturn the ranking, but it warrants caution before over-committing. All reviews are self-selected public app reviews; cancellation references are expressed intent only, not confirmed churn. The pack also shows 97 quarantined records out of 660,622 source rows, with 0 pending.

## What to measure next quarter

Track the top issue's monthly share_pct and complaint volume to see if it continues the rise seen through 2023-10 (29.31%). Test 2-3 concrete product changes: (1) trial restoring or partially enabling select controls (e.g., limited skips) for free-tier users in an experiment; (2) add in-app messaging that clearly flags Premium-only limits *before* a free user hits them, rather than mid-action; (3) pilot a revised free-tier onboarding flow explaining locked features upfront. Treat all of these as hypotheses to validate, not confirmed fixes, and monitor cancellation-intent mentions and severity for this issue alongside the independent verifier's agreement rate to confirm the signal holds.
