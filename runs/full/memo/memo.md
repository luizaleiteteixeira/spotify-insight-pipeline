<!-- run full | model claude-sonnet-5 | prompt memo_v4@083aa894 | automatic check passed: True | human review: APPROVED by Luiza Leite Teixeira (author) on 2026-10-07 (see docs/memo_review.md) -->

# Decision Memo: Next Quarter Product Priority

## Recommendation

Spotify should spend next quarter's product effort fixing the free-tier experience around Premium-only controls, not chasing playback bugs or support tickets. This single issue (ISS-BILLING-PREMIUM_ONLY_CONTROLS) has 58940 complaints [C001] and a severity sum of 174087 [C002], the highest priority score of any issue in the pack, well ahead of the next-largest actionable issue. It also carries 11558 cancellation-intent mentions [cancellation_intent_count on ISS-BILLING-PREMIUM_ONLY_CONTROLS], expressed directly in reviews rather than confirmed churn, and the issue is accelerating sharply in recent months (see Evidence). The issue_definitions entry for this issue describes it as covering "song choice, skips, rewind, order, lyrics" and similar functions locked behind Premium — in other words, this is a free-tier packaging and pricing decision (which controls the free tier allows), not a payment-processing or support failure. It is classified under billing only by the course's shared topic-label rule. The recommendation is therefore product design work on the free-tier experience, not a billing-operations fix.

## Evidence

- Area-level totals: billing_support leads on severity_sum (204819) and cancellation mentions (12766), ahead of usability (178198 / 6840), none (147625 / 6408), playback (142045 / 3882), content (53862 / 1315) and access (48943 / 810).
- The top issue alone, ISS-BILLING-PREMIUM_ONLY_CONTROLS, accounts for the majority of billing_support's severity_sum: 174087 [C002] of the area's 204819.
- Representative complaints describe being blocked from basic controls: one user wrote "I am not to move the section of the song" (review `8964aaea-f4c7-4b01-94e0-a76b992018fd`), and another said "I literally cannot select songs and have to queue them to play anything" (review `d8be33b8-6758-4ae7-9e3e-6e7d02163930`).
- Monthly trend (comparable full months, per `top_issue_monthly`): share_pct held in a 3.28%-4.76% band from 2022-06 through 2023-05, then rose to 7.97% in 2023-06, 17.04% in 2023-09, and peaked at 29.22% in 2023-10. Note 2022-05 and 2023-11 are partial months, and review volume varies considerably by month (e.g. 2023-07 had 85005 reviews vs 20837 in 2023-02).

## Alternatives considered

- **Usability**: ads (ISS-USABILITY-ADS) total 37406 complaints [C009], severity sum 85318 [C010]; layout redesign (ISS-USABILITY-LAYOUT_REDESIGN) totals 18938 complaints [C017]. Both trail the top issue substantially and usability's area mean_severity (2.368709) is lower than billing_support's (2.890637).
- **Playback**: has the highest sev4plus count among areas (11374) but its top issue, stops/skips, totals only 13827 complaints [C013] and severity sum 44042 [C014] — a fraction of the top issue's 174087 [C002].
- **Access**: highest area mean_severity (3.838968), but lowest complaint_count (12749) and login failures total just 8029 complaints [C025].
- **General/other dissatisfaction** (ISS-OTHER-GENERAL) ranks second with 73774 complaints [C005] and severity sum 147625 [C006], but its definition spans "general praise, generic criticism, unrelated or unclear text," so it offers no specific, actionable product fix.

## Risks and limitations

Label reliability is imperfect: the human golden-set of 50 cases shows topic_accuracy of 0.7, and on that small sample 0 cases were human-labeled billing while the model labeled 5 as billing, suggesting some risk of topic misclassification. An independent verifier on a larger 2000-review sample found topic_agreement_all of 0.8475, and specifically on the top issue, 194 of 252 verified cases agreed on topic. Given the size of the lead this issue holds in complaint_count and severity_sum over every other issue, this reliability picture does not plausibly overturn the ranking, though some individual review labels may be wrong. All counts come from self-selected public app reviews; cancellation figures reflect expressed intent in review text only, not confirmed subscription cancellations, revenue, or churn. No records are reported pending or quarantined beyond the 1596 quarantined rows noted in scope.

## What to measure next quarter

- Track share_pct and complaint_count for ISS-BILLING-PREMIUM_ONLY_CONTROLS monthly to see if product changes bend the recent upward trend (7.97% to 29.22% across 2023-06 to 2023-10).
- Test specific product options: (1) trial restoring limited on-demand track selection or skip allowances for free-tier users in a subset of markets, (2) surface clearer in-app messaging before a free user hits a Premium-only limit (e.g., contextual explainer when attempting to skip or select a song), and (3) test a lighter-weight "preview" of Premium controls for free users to reduce surprise-driven complaints. These are options to test, not proven fixes.
- Monitor cancellation-intent mentions tied to this issue as a leading indicator of dissatisfaction, understanding it is expressed intent, not confirmed churn.
