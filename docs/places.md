# Places, directions and booking

Ask Chief what's around you and how to get there, from the dashboard, the phone page or Telegram.

```
sushi near me
I'm at Union Square, San Francisco
any pharmacy open now near me
directions to the first one
how do I get to the Ferry Building by bike
book a table at number 2 for 4 at 8pm
book tickets for Dune tomorrow at 7pm
forget my location
```

## Where you are

Chief only knows your location when you give it:

- **Dashboard**: the pin button in the ask bar. If you ask something that needs it, your browser asks first.
- **Phone page**: the *Share my location* button. Browsers only share a location on a secure page (https, or
  `localhost` on the same computer). Over plain `http://` on your Wi-Fi, use Telegram or say where you are.
- **Telegram**: when a question needs it, Chief shows a *Share my location* button. Tap it and Chief answers
  the question you asked.
- **Say it**: "I'm at MG Road, Bengaluru".

Your location is kept in memory only: never written to disk, the demo cache or the activity log as more than a
point rounded to about 1 km. It is forgotten after 6 hours, on restart, or when you say "forget my location".

## Where the data comes from

Free OpenStreetMap services, no key needed. Map data (c) OpenStreetMap contributors.

| Service | Used for | Sent |
|---|---|---|
| Overpass | places by type and cuisine | your point rounded to about 100 m |
| Nominatim, then Photon | places and addresses by name | what you asked for, and a box around your point rounded to about 100 m |
| routing.openstreetmap.de (OSRM) | walking, cycling and driving routes | start rounded to about 10 m, and the destination |

Opening hours come from OpenStreetMap and are only read out when they're plain (`Mo-Fr 11:00-22:00`); holidays
and seasons are left unsaid rather than guessed. Set `CHIEF_PLACES=off` to stop all lookups. In mock mode Chief
uses made-up sample places and says so.

## Booking: Chief gets it ready, you finish it

Chief finds the place, the time and how many of you, and gives you the restaurant's own booking page (or a search
for one) and its phone number. It **never** books, signs in or pays for you: you tap *Reserve* or *Call* and finish
there, payment included. If you approve the card, the only thing Chief does is put the plan on your calendar,
marked "Not booked by Chief", through the same approval gate as every other change.

Tickets work the same way: *Find tickets* opens a search; tell Chief the time and it can add it to your calendar.

## What it can't do yet

- Live bus and train times (the Directions link opens transit directions in Google Maps instead).
- Ratings, prices and photos: OpenStreetMap doesn't have them. A Google Places key could add them later.
- Booking for you: by design.
