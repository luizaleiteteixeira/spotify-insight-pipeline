<!-- run pilot-warm | model claude-sonnet-5 | prompt memo_v2@d321a25a | automatic check passed: True | human review: PENDING -->

# Decision Memo: Next Quarter Product Priority

## Recommendation
Focus next quarter's product effort on **billing/support**, specifically the issue of free users losing playback controls (ISS-BILLING-PREMIUM_ONLY_CONTROLS). This issue has the highest priority score in the pack: 12 complaints [C001], severity sum 36 [C002], mean severity 3.0 [C003], priority score 36 [C004]. Billing_support is also the top area by severity sum (38, area table), ahead of usability (26), none (24), playback (18), content (17), and access (16).

## Evidence
The issue centers on an app update that restricted playback controls (shuffle, song selection) to Premium subscribers, generating visible frustration. Representative quotes include `21915fcd-9a35-48ba-b277-6296c4154603` ('made everthing premium and so usless'), `5c9c17d3-9d6a-4fa1-a2a4-ae98eefbbd25` ('it plays any song of the Playlist randomly'), and `7084ced1-0428-4093-9328-442f74719b4f` ('removed basic features of music player'). These are consistent, specific complaints about a defined feature change rather than vague dissatisfaction, which makes the issue more actionable than, for example, ISS-OTHER-GENERAL (12 complaints [C005], priority score 24 [C008]), whose examples ('Worst app', 'Worst app.') lack diagnostic detail.

## Alternatives considered
- **Usability**: ISS-USABILITY-ADS ranks third overall with 8 complaints [C009] and priority score 19 [C012]; ad load and UX redesign complaints (ISS-USABILITY-LAYOUT_REDESIGN, priority score 7 [C024]) are real but lower-scored than the billing issue.
- **Access**: Smallest in volume (2+2 complaints [C013][C017]) but highest mean severity in the area table (4.000000) and all 4 access complaints are sev4plus (area table). Worth monitoring, but current volume doesn't yet outrank billing on priority score.
- **Playback**: Weakest evidence; area severity sum is 18 and no playback issue reached the top_issues list.
- **Content (context only)**: Lyrics and catalog gaps (ISS-CATALOG-LYRICS, ISS-CATALOG-MISSING_CONTENT, each priority score 6 [C028][C032]) are low-severity and low-volume; useful context but not a priority driver.

## Risks and limitations
This analysis is based on 100 self-selected public app reviews (scope: source_rows 100, completed 100, quarantined 0, pending 0) spanning 2022-05-17 to 2023-11-15, with no quarantined or pending records to flag. These are not representative of the full user base, and contain no revenue, subscription-tier, or confirmed-churn data. Cancellation language (e.g., in ISS-OTHER-GENERAL and ISS-USABILITY-ADS examples) reflects stated intent only, not verified cancellations. Severity scores are self-reported sentiment proxies, not business-impact measures. The access area's high mean severity (4.000000) is based on only 4 complaints total and could shift quickly with more data.

## What to measure next quarter
Track complaint volume and severity for ISS-BILLING-PREMIUM_ONLY_CONTROLS specifically (currently 12 complaints [C001], severity sum 36 [C002]) to confirm whether product changes reduce this issue. Simultaneously monitor the access area's sev4plus rate (currently 4 of 4 complaints, area table) since its severity profile, if volume grows, could justify re-prioritization. Continue tracking ISS-USABILITY-ADS complaint count (8 [C009]) and the undiagnosed ISS-OTHER-GENERAL bucket (12 complaints [C005]) to see if clearer sub-issues emerge from future reviews.
