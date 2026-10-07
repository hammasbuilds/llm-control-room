# Motion research and decisions

Sources
- Material Design 3 motion tokens: durations short1 50 ms to long4 600 ms, medium1 250 ms, medium2 300 ms; easing
  emphasized `cubic-bezier(.2,0,0,1)`, emphasized-decelerate `(.05,.7,.1,1)`, accelerate `(.3,0,.8,.15)`.
  https://m3.material.io/styles/motion/easing-and-duration/tokens-specs , https://material.io/design/motion/customization.html
- Apple HIG, Motion: motion should communicate and give feedback, stay brief, never block input, and respect
  Reduce Motion. Feedback fires on pointer-down; interrupted animations start from their live value.
  https://developer.apple.com/design/human-interface-guidelines/motion
- Stripe / Linear / Apple / Vercel practice (summary): hover and press 120-200 ms, state change 180-260 ms,
  popover and toast 220-320 ms, section entrance 400-800 ms; never `linear` except looping indicators.
  https://claudepluginhub.com/skills/rushyop-better-ux-quality-plugins-better-ux-quality/animation-systems
- Vercel (Geist), Grafana and Raycast: ops consoles use restrained motion, a single accent glow, and put the
  motion on state (busy, healthy, new data) rather than decoration.

Tokens used here: `--dur-fast` 120 ms, `--dur-base` 200 ms, `--dur-slow` 320 ms; `--ease-std` (.2,0,0,1),
`--ease-out` (.05,.7,.1,1), `--ease-spring` (.34,1.56,.64,1) for press spring-back. Press scale .97, hover lift
1-2 px; transform and opacity animate (plus box-shadow on hover). Stagger 40 ms per item, capped at 10 items.

What was added (end of `style.css`, and `static/motion.js`)
- Buttons: hover lift and brighten, press scale .97 with spring-back, pointer-origin ripple, spinner while
  disabled (busy), success (lime) or error (magenta) ring flash after an action.
- Cards and tiles: hover lift, pointer-following cyan spotlight (fine pointers only), staggered entrance.
- Inputs: animated accent focus ring, shake when invalid, chips pop when selected.
- Feedback: stacked toasts that slide in and out, KPI count-up, bars grow from zero, skeleton shimmer.
- Delight: scanning glow across the Try-it panel while a request runs, and a typing reveal of the answer.
- Reduced motion: all CSS animation and transitions off, JS count-up, typing and ripple skipped; state stays visible.
