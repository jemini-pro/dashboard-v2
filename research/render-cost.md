# What the animated blur dashboard costs in Chrome

Tags: **[D]** documented, **[M]** measured, **[I]** inference to validate. No benchmark was run. Subject: `index.html`, `style.css`, `app.js` at `ae0cb76`.

## What the page asks the compositor to do

**Blobs.** `.bg-blobs` is a `100% × 100%` element with `filter: blur(140px)`, `opacity: .85`, `overflow: hidden`, `z-index: -1`; its three 400–550 px children run infinite `transform` animations over 20–30 s (`style.css:63-146`).

- CSS `blur(<length>)` is the Gaussian **standard deviation**, so σ=140 [D: [Filter Effects 1](https://www.w3.org/TR/filter-effects-1/)]. Skia expands blur bounds by **3σ** (420 px) and clamps σ at 532 [D: [SkBlurImageFilter.cpp](https://github.com/google/skia/blob/main/src/effects/imagefilters/SkBlurImageFilter.cpp)]. The raster engine caps σ at 135 and rescaling above it is "about 6-7x faster on large blurs" [D: [SkBlurEngine.cpp](https://github.com/google/skia/blob/main/src/core/SkBlurEngine.cpp)]; GPU linear blur supports only σ≤4 [D: [SkBlurEngine.h](https://github.com/google/skia/blob/main/src/core/SkBlurEngine.h)]. σ=140 is large on every path and forces downscale/upscale over a viewport-sized surface [I].
- The filter is on the **container**. A filter applies to the element's rendered subtree (`SourceGraphic`) [D: Filter Effects 1], and the children's transforms change that subtree every frame. In cc, filters are render-pass effects: the subtree draws into a texture and the effect is applied [D: [How cc works](https://chromium.googlesource.com/chromium/src/+/main/docs/how_cc_works.md)]. A non-root pass is skipped only when undamaged and cacheable [D: [direct_renderer.cc](https://chromium.googlesource.com/chromium/src/+/main/components/viz/service/display/direct_renderer.cc), `CanSkipRenderPass`], and an animated blob is damage every frame [I]. So the full-element filter is recomputed every frame, though 25 s drift moves only ~0.1 px/frame at 60 Hz [I]. Dominant cost.
- A static `filter` is **not** a compositing reason; only `kActiveFilterAnimation` and `kWillChangeFilter` are [D: [compositing_reasons.h](https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/renderer/platform/graphics/compositing_reasons.h)]. A static filter can be rastered once [I]; the drift defeats reuse [I]. `will-change` will not help: animated properties already behave as if set [D: [MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/will-change)].

**Frosted cards.** `backdrop-filter` *is* a compositing reason [D: compositing_reasons.h]. The spec copies the backdrop into a temporary buffer and filters it [D: [Filter Effects 2](https://drafts.csswg.org/filter-effects-2/)]. Each application needs a separate rendering pass; the naive version "would double the required rendering time" and memory/GPU bandwidth [D: Filter Effects 2]. Five backdrop rules and ~30 DOM elements (~8–12 on screen) [I]. Because the backdrop contains the animated blobs, every visible copy is new each frame [I]. The sidebar alone is 350×1080 px. Backdrop filters also set `lcd_text_disallowed_by_backdrop_filter` [D: [effect_node.h](https://chromium.googlesource.com/chromium/src/+/main/cc/trees/effect_node.h)], losing subpixel LCD text [I].

**Universal transition.** `* { transition: … }` (`style.css:44-49`) is nearly free at rest: transitions start only when a computed value changes, and `innerHTML`-created elements have no before-change style [D: [CSS Transitions 1](https://drafts.csswg.org/css-transitions-1/)]. Costs: a 0.5 s all-element paint burst at the theme flip and a paint-heavy card hover (only `transform` is compositor-only) [D: [web.dev animations guide](https://web.dev/articles/animations-guide)]. `.metric-fill { transition: width }` does not fire today, but would run layout every frame for 0.5 s after each poll if updates became in-place [I].

**10 s rebuild.** `deviceGridEl.innerHTML = ...` (`app.js:273-275`) destroys and recreates device cards and their backdrop layers, so expect a layout/paint/composite spike every poll [I].

**Cost location.** Transform/opacity are compositor-only [D: web.dev], and cc ticks transform, opacity and filter animations on the compositor [D: How cc works]. A JS profile looks idle while the GPU is busy; read the Frames/GPU tracks [D: [DevTools Performance reference](https://developer.chrome.com/docs/devtools/performance/reference)].

## Order of magnitude [I]

At 1920×1080 DPR 1 the filtered container is ~2.1 Mpx before 3σ outsets; downscale + blur + upscale at 60 Hz is ~10^8–10^9 pixel ops/s, quadrupled at DPR 2. Add ~8–12 backdrop copies (cards ~28k px, sidebar 378k px) with σ 12–20, each with its own pass, every frame. Plausibly a few percent of a discrete GPU, 10–30% of a laptop iGPU, and enough to keep the package out of its lowest power state. The display backlight still dominates battery; the controllable part is whether the GPU idles. Needs [M] on both machines.

## Techniques that preserve the look

1. **Bake the blobs.** A blurred disc is visually a radial gradient. Replace `filter` with `background: radial-gradient(...)` on three positioned divs and keep the transform drift (compositor-only). Removes the filter; motion and colour survive [I].
2. **Or move the filter onto each `.blob`.** A filter applies to the element's own rendering, then its transform positions the result [D: Filter Effects 1]. The filter input is then constant in layer space, so the blurred texture is reusable and blob-sized [I]. `blur(47px)` with `scale(3)` approximates σ=140 with ~9× fewer filtered pixels [I].
3. **Cut backdrop filters.** Keep the sidebar, where frost is largest, and give cards translucent fills; or remove all. Blur is low-pass, so over smooth gradients it changes little; an rgba fill also hides the dot pattern [I; screenshot diff].
4. **Quantise motion.** A 25 s drift needs 10–15 updates/s, not 60/120. Step the transform from JS (or toggle `animation-play-state`) so the page is static between updates; CSS animations otherwise tick every vsync [D: How cc works]. Gate motion behind `prefers-reduced-motion` [D: [web.dev](https://web.dev/articles/prefers-reduced-motion)]; add a separate low-power flag.
5. **Skip offscreen work.** `content-visibility: auto` + `contain-intrinsic-size` on `.category-section` skips layout/paint offscreen [D: [web.dev](https://web.dev/articles/content-visibility)]; `contain: layout paint style` on cards limits invalidation [D: [MDN](https://developer.mozilla.org/en-US/docs/Web/CSS/CSS_containment/Using_CSS_containment)].
6. **Stop rebuilding.** Update text/width in place, keep layers alive, and pause polling while `document.hidden`; hidden pages stop producing frames [D: [Chrome timer throttling](https://developer.chrome.com/blog/timer-throttling-in-chrome-88)].
7. **Scope the transition** to interactive elements; drop colour/background/shadow from `*` and `transition: all`. Paint transitions are the documented warning [D].
8. **Do not add `will-change: filter` blindly**; it costs memory and is a last resort [D: MDN]. Test as a control.

## Variants to prototype

Use an orthogonal `?fx=`; keep today's default.

| Variant | Change | Expected saving | Measurement |
| --- | --- | --- | --- |
| `fx=base` | Current page | Baseline | All methods |
| `fx=bake` | Gradient blobs, no filter, drift kept | Removes full-viewport filter; likely largest GPU win | GPU ms/frame, energy |
| `fx=perblob` | Filter per blob, smaller σ scaled | Large; filter input constant under transform | GPU ms/frame, screenshot |
| `fx=nofrost` | Backdrop only on sidebar | Removes ~6–10 passes/frame | GPU ms/frame, layers |
| `fx=nofrost-all` | Translucent fills everywhere | Removes all backdrop passes | GPU ms/frame, visual diff |
| `fx=lowfps` | Drift at 10–15 Hz | Up to 4–6× fewer animated frames | Trace frame count, energy |
| `fx=static` | No drift/transitions | Floor for animation cost | Energy, idle GPU |
| `fx=dom-diff` | In-place updates, pause when hidden | Removes 10 s spikes | LoAF, per-poll frame time |
| `fx=scoped-transitions` | Global transition scoped | Small at rest; removes theme burst | Animations track, trace |
| `fx=cv-auto` | `content-visibility: auto` on sections | Skips offscreen work | Layer count, GPU ms/frame |

## Instrumentation recipe

**Steady state.** DevTools Performance: record 20–30 s and read **Frames** (green/yellow/red), **GPU** and **Animations**; non-composited animations get red triangles [D: Performance reference]. Enable the **FPS meter** and **layer borders** in Rendering [D: [Rendering reference](https://developer.chrome.com/docs/devtools/rendering/performance)]; paint flashing shows paint, not compositor-side filter work [I]. For attribution, `Tracing.start` with `cc`, `viz`, `gpu`, `blink`, `disabled-by-default-devtools.timeline`, then sum `DrawFrame`, `SkiaRenderer::DrawRenderPassQuad` and `Graphics.Pipeline` [D: [CDP](https://github.com/ChromeDevTools/devtools-protocol/blob/master/json/browser_protocol.json); categories per [trace_events.md](https://chromium.googlesource.com/chromium/src/+/main/docs/trace_events.md)].

**Main thread.** Over CDP, diff `Performance.getMetrics` over five minutes: `TaskDuration`, `ScriptDuration`, `LayoutDuration`, `RecalcStyleDuration`, `JSHeapUsedSize` (names from [inspector_performance_agent.cc](https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/renderer/core/inspector/inspector_performance_agent.cc)). They do not show GPU cost; use them for rebuilds and theme flips. In-page, `PerformanceObserver` `long-animation-frame` catches rebuild spikes (Chrome 123+) [D: [LoAF](https://developer.chrome.com/docs/web-platform/long-animation-frames)].

**Variant control.** `Emulation.setEmulatedMedia` with `{name:"prefers-reduced-motion", value:"reduce"}` switches motion. For visual comparison, freeze animations with `Animation.setPlaybackRate(0)` and `Animation.seekAnimations`, then `Page.captureScreenshot` and diff; slow drift otherwise makes every pair different.

**Energy.** Windows: Task Manager GPU and power usage; `powercfg /energy /duration 120` and `powercfg /batteryreport` [D: [Microsoft](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options)]. macOS: Activity Monitor Energy [D: [Apple](https://support.apple.com/guide/activity-monitor/view-energy-consumption-actmntr/mac)] plus `powermetrics --samplers gpu_power,cpu_power -i 1000` and `pmset -g batt`. Run each variant 30 min on battery, fixed brightness, no other apps, DevTools closed and unoccluded; alternate variants; report medians. Record `devicePixelRatio`, window size and rAF interval; 120 Hz roughly doubles per-second cost versus 60 Hz [I].

## What this means for the dashboard

1. The cost is a viewport-sized 140 px filter recomputed every displayed frame, plus one backdrop pass per visible frosted element—not JavaScript or the clock.
2. Highest value: bake the blobs into gradients, or move the filter onto the blobs. That removes the dominant pass while preserving drift and the glassy background.
3. Second: treat `backdrop-filter` as a per-element tax; keep the sidebar, replace card frost with a translucent fill, and verify with frozen screenshots.
4. Third: decouple motion from power with 10–15 Hz drift, a `prefers-reduced-motion` path, and an explicit low-power mode.
5. Stop rebuilding the device grid; keep nodes and layers alive. Scope the global transition.
6. Judge changes on GPU ms/frame and battery %/h together; the display dominates total energy.

## Open questions

- Actual GPU ms/frame and package watts on JEM-PC and the MacBook, at which GPU/DPR/refresh rate?
- Does Chrome reuse the blurred texture when only the blob's transform animates?
- Is 10–15 Hz drift perceptually identical at 20–30 s durations on both displays?
- Do gradient blobs and translucent card fills pass a frozen screenshot diff?
- Does `content-visibility: auto` matter at 1080p, given how many cards are offscreen?
