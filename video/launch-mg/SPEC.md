GOAL:    five inserts for a 60 s launch video; cut between the founder's face shots, each one a poster that
         reads in 2.5 s and holds; the viewer leaves knowing: alerts pile up, old tools fail, Kavach decides by
         policy and every claim can be re-run.
OUTPUT:  HyperFrames, 1920 x 1080, 30 fps, five scenes rendered as one 29.5 s film and as five files
TIME:    mg1 5.5 s (165 f), mg2 5.5 s (165 f), mg3 7 s (210 f), mg4 7 s (210 f), mg5 4.5 s (135 f); no music
         in the files (the editor adds the bed), so cuts land on the editor's grid, not a beat
LOOK:    two worlds. The problem (mg1, mg2 first half): ground #12171E flat, ink #E7EAEF, cold grey #9AA5B3,
         no accent. Kavach (mg2 second half to mg5): paper #EEF0F3 flat, ink #141A21, grey #6B7684, one
         accent #C2491A on one element per frame (the answer box, the mark, one line). The flood from dark to
         paper is the one colour change in the set (mg2 at 2.6 s, 0.4 s, power2.inOut).
         display: Oswald 700 uppercase, tracking -0.01em, hero lines 150 to 380 px, long lines 92 to 110 px;
         utility: IBM Plex Mono 400, 30 to 44 px, never under 30 px. Left margin 120 px for every stacked
         line; the wordmark and the end card are centred. No gradients, no glow, no grain, no shadows: ink on
         a flat ground, like a printed case file. The Kavach mark is a 0.42 em orange square after the wordmark.
OBJECTS: the alert queue (four lines stacking down the frame), the three failures (three cold lines), the
         wordmark KAVACH plus its orange square, the pipeline (five outlined boxes 270 x 150 joined by
         drawn-on arrows, the last box filled orange), the proof (one hero numeral at a time, 380 px).
MOTION:  enter: blur 8 to 0 px with opacity 0 to 1, 0.13 s per word 0.067 s apart (lines), or a slam, scale
         1.12 to 1 with blur 10 to 0 in 0.35 s power4.out (hero numerals and the first line of a scene);
         letters of the wordmark 0.08 s apart, opacity 0.3 to 1 with blur 6 to 0, 0.3 s power4.out; the mark
         scales 0 to 1 in 0.25 s back.out(1.4). exit (only inside mg4's stage swaps): blur 0 to 8 and opacity
         to 0 in 0.15 s power3.in. arrows draw with stroke-dashoffset 0.3 s power2.inOut. count-ups 1.2 to
         1.4 s power2.out then hold. Landed text never moves. Nothing loops, breathes or drifts.
SHOTS:   mg1 0-165: four lines land at 0.1, 1.1, 2.1, 3.2 s; each landing dims the earlier lines to 55%;
                    reads: ALERTS PILE UP / MINUTES PER CASE / A WRONG BLOCK LOSES A GOOD CUSTOMER /
                    A MISSED RING COSTS THE BANK; holds 2.0 s on the full stack.
         mg2 0-165: three cold lines at 0.1, 0.7, 1.3 s; hold to 2.6; the lines blur out, the ground floods
                    to paper, KAVACH cascades in letter by letter from 2.8, the mark lands at 3.5, the line
                    AN AI FRAUD INVESTIGATOR at 3.8; hold.
         mg3 0-210: kicker HOW IT WORKS from frame 0; boxes ALERT, GRAPH EVIDENCE (TigerGraph), DETECTORS,
                    POLICY, ANSWER pop at 0.2, 0.8, 1.4, 2.0, 2.6 with the arrows drawing between; ANSWER
                    fills orange at 2.9; line DECISIONS BY POLICY. THE MODEL ONLY WRITES. at 3.6; line
                    EVERY CLAIM RE-RUNNABLE in accent at 5.2; hold.
         mg4 0-210: kicker THE PROOF from frame 0; stage 1 (0 to 1.9): 18 counts up, "/ 18", label on the
                    organizer's grader, no errors; stage 2 (1.9 to 3.6): 0 slams in, label legitimate customers
                    harmed; stage 3 (3.6 to 5.3): 650,938 counts up, label transactions in TigerGraph;
                    stage 4 (5.3 to 7): EVERY CLAIM RE-RUNNABLE in accent with the line re-run against the
                    graph: 0 failing; hold.
         mg5 0-135: KAVACH cascades from 0.1, mark at 0.85, tagline AN AI FRAUD INVESTIGATOR A BANK CAN RUN
                    at 1.1, BUILT ON TIGERGRAPH in accent at 1.7, HACKER HOUSE GOA 2026 at 2.0; hold 2.5 s.
BANNED:  dark void with small floating elements (hero lines fill 60 to 90% of the width); glow, bloom, neon,
         particles; gradients; glass cards; everything centred (only the wordmark scenes are); fade-and-slide-
         up entrances; titles that keep moving after they land; breathing or pulsing loops; corner labels and
         HUD readouts; three cards in a row and KPI grids (one numeral at a time instead); a docked logo.
QA:      mg1 f=160: four lines, last one full ink, the rest dimmed, nothing clipped at the right edge.
         mg2 f=78 (2.6 s): the three lines still readable just before the flood; f=120: paper ground, KAVACH
         complete with the mark, tagline landed. mg3 f=100: five boxes, four arrows, ANSWER orange; f=200: two
         lines under the pipeline. mg4 f=50: "18 / 18" landed with its label; f=150: 650,938 fully counted.
         mg5 f=130: wordmark, tagline, BUILT ON TIGERGRAPH, HACKER HOUSE GOA 2026, all still.

V2 (13:50 IST): every scene carries a mechanism besides the type. mg1: an alert queue of 18 cards on the right (600 x 60,
     mono 30 px, cold stroke) sliding in from x+240 with gaps shrinking from 0.45 s to 0.08 s, piling past the top of a
     clipped 980 px column. mg2: a 6 x 6 dot field (r 9) at the right, a RULES box (216 px) drawing around its top-left
     3 x 3, a ring of six bright dots (r 11) popping outside it with back.out; KAVACH's mark is a shield (150 x 175,
     outline draws 0.35 s, stamps in from scale 1.35 and -12 degrees with back.out(1.4), fills in 0.18 s, one ring
     0.6 to 0 in 0.35 s). mg3: an orange token (r 14) rides the arrow line behind the boxes, 0.3 s per hop,
     power2.inOut; a six-node graph (edges draw, nodes pop) above GRAPH EVIDENCE; two cards flag orange with a ring
     edge at DETECTORS; the rule label above POLICY; the token shrinks as ANSWER fills. mg4: 18 ticks (48 px) fill on
     the frame the count first shows each value; nine card outlines with a drawn check each (0.18 s, 0.09 apart);
     a 4 x 36 dot field filling in reading order over the 1.4 s count; a six-row ledger of real claims with RE-RUN OK
     landing 0.1 s apart. mg5: the shield draw, stamp and fill; a 400 px rule drawing under BUILT ON TIGERGRAPH.
