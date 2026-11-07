# Walking guide: spoken turn by turn

After a walking directions answer ("sushi near me", then "directions to the first one"), tap the big
**Guide me** button, or say **"guide me there"** / **"start walking"**. Chief then talks you there from your live
location:

- the first instruction, once: "Head north on Powell Street for 230 metres."
- each turn about 45 m before it, and again at it: "In 40 metres, turn right onto Post Street." ... "Turn right onto Post Street."
- on a long stretch with no turn coming (every 250 m walked): "Keep going straight on Market Street. In 300 metres, turn left ..."
- within 20 m of the end: "You've arrived at Sakura Sushi Bar."
- walking the wrong way (about 30 m further from the next turn than you'd been): a fresh route from where you are,
  at most one every 20 seconds and 10 per walk.

While guiding, the page shows **"Watch the road, not the screen"**, the current instruction in large type and a big
**Stop** button, and keeps the screen awake where the Wake Lock API exists (Chrome on Android, Safari 16.4+). Say
"stop guiding" to stop by voice. GPS and voice only: no camera.

On **Telegram**, say "guide me there" after directions, then share a **live** location: Chief sends the next
instruction near each turn and nothing in between.

## How it works

| Piece | What it does |
| --- | --- |
| `chief/places.py` | `Route.points`: every instruction as a `Step` with its place (OSRM `maneuver.location`) and the distance to the next one. Sample routes in mock mode are an L: along Powell Street, then a real corner, then Post Street. `Route.steps` (the text) is unchanged. |
| `chief/skills/directions.py` | Each item of the answer also carries `say`, `lat`, `lon`, `distance_m`, `kind`; the first item has `guide: true` on walking routes. Only fields were added. |
| `chief/guide.py` | `next_cue(guide, lat, lon, accuracy)`: pure, decides what to say. `start` / `tick` / `stop` wrap it and fetch a new route when needed. |
| `chief/skills/guide.py` | "guide me there", "start walking", "walk me to ...", "stop guiding". |
| `chief/web/app.py` | `POST /guide/start`, `POST /guide/tick`, `POST /guide/stop`, `GET /guide.js`: all need the token like every other route. |
| `chief/web/guide.js` | Shared by `index.html` and `dashboard.html`: the Guide me button, the guiding bar, `watchPosition`, the Wake Lock. It speaks through each page's own `speak()`. |
| `chief/telegram_app.py` | `_on_live`: while a guide is active, sends what `tick` says. |

**Why the logic is on the server.** One copy, in Python, tested with the rest of Chief; the web page and Telegram
share it; and a wrong turn can fetch a new route in the same request instead of another round trip. The cost is
one small POST every few seconds while guiding, which is fine for walking speed. If the connection drops, the page
says "Lost the connection. Still trying..." and carries on when it's back.

**Privacy.** The guide (route, turn places, what was said) lives on `Whereabouts` in memory only, like your
location. "Forget my location" clears it, and arriving or Stop drops it. `guide` is in `gate.WHERE_SKILLS`, so the
diary never keeps its answers. Positions go to the router rounded as before (about 10 m).

## Trying it without walking

Mock mode (`CHIEF_MODE=mock`), any free port except 8765. In the browser console on the phone page:

```js
// after "directions to the first one" with a location shared at Union Square (37.7880, -122.4075)
await ChiefGuide.start(null, { lat: 37.7880, lon: -122.4075 });  // a given start point turns the GPS watch off
await ChiefGuide.simulate(37.78975, -122.4075);  // "In 40 metres, turn right onto Post Street."
```

## Limits, and what needs a real phone outdoors

- Turns are judged by straight-line distance to the turn point (the route's full shape isn't fetched). A road that
  curves away from the next turn could look like the wrong way; the 30 m margin and the 20 s re-route pause soften it.
- GPS in city canyons wanders 10 to 30 m. Positions with accuracy worse than 50 m are ignored; a turn taken
  without passing within 15 m of its point is still noticed when you're on the stretch after it.
- To check outdoors: announce timing at walking pace, false "wrong way" on curved streets, re-route wording,
  arrival radius, the Wake Lock on iPhone and Android, speech while the screen is locked (browsers usually stop
  geolocation and speech in the background, so keep the page open), and Telegram live location update rate.
