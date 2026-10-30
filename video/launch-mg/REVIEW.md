# Review: Kavach launch inserts (v2, with mechanisms)

Five motion-graphic inserts (29.5 s in one file, also cut into five files) for the 60 s launch video on X. They sit between the founder's face shots; the shot list is `docs/round2/LAUNCH-VIDEO.md`. v2 adds a moving mechanism to every scene after the founder asked for more than type.

## Creative calls
- Two worlds, one colour change: the problem on a near-black ground in cold grey type (mg1, the first half of mg2), Kavach on paper with ink and one orange accent (from the flood in mg2 to the end card). The flood at mg2 2.6 s is the only ground change in the set, so it reads as the turn of the story.
- Each mechanism is the thing the line talks about, drawn literally: mg1 an alert queue of 18 cards sliding in with shrinking gaps until the pile runs past the top of the frame; mg2 a field of transaction dots, a RULES box that catches its square, and a ring of six bright dots forming outside it, then KAVACH with a shield mark that draws its outline, stamps in with a 12 degree settle and fills, plus one fading impact ring; mg3 an orange token that rides the arrow line and passes behind each box (ALERT to ANSWER), a six-node graph growing above GRAPH EVIDENCE, two of its cards flagging orange with a ring edge when the token reaches DETECTORS, the rule label above POLICY, the token becoming the orange ANSWER box; mg4 eighteen ticks filling on the exact frames the count changes, nine customer cards each getting a drawn check, a 144-dot field loading with the 650,938 count, and a ledger of six real claims from the cases each getting its RE-RUN OK; mg5 the shield draw, stamp and fill before the lockup, and a rule drawing under BUILT ON TIGERGRAPH.
- Each insert still reads as a poster within about 2.5 s and then holds, because the editor will cut in and out at any point; nothing drifts after landing.
- Not built: a music bed and sound hits (the editor adds the bed under the face shots and the inserts alike); a narrated version.

## Measured and looked at
- `npx hyperframes check --json`: 0 lint, runtime, layout, motion or contrast findings. Craft warnings kept on purpose: `first_frame_empty` (line 1 slams in at 0.1 s; the combined file is not the posted video) and `film_silent` (sound is added in the edit). The queue cards and the ledger rows are marked `data-layout-ignore`: they are texture to be skimmed while the hero line is read, not lines to read in full.
- Looked at 12 frames across the five scenes after the mechanisms went in, then 4 more after the fixes (line widths against the card column, card width for the longest amount, the RULES box clear of the fourth dot column, the token on the arrow line behind the boxes, the shield at 150 x 175).
- Count-ups and the tick fills are per-frame `set()` rows and tweens placed on the frame where the count first shows each value, so every seek shows the same state.
- Not checked by anyone: playback in the founder's editor; the files were only decoded with ffmpeg.

## Weakest shot
mg2's diagram sits in the right third next to three long lines; at phone size the dots are small. With more time: hold the lines at the left and give the diagram the whole right half at twice the scale.

## Warnings kept
- `first_frame_empty`: see above.
- `film_silent`: by design for inserts.
