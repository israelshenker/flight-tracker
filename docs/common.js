// Shared by index.html (owner's page) and trip.html (friends' page). Loaded first by both, as a
// plain script, so these are page globals. Bump ?v= in both pages' <script src> when this changes:
// GitHub Pages lets browsers cache it for 10 minutes.
const $ = id => document.getElementById(id);

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const timeLabel = hhmm => { if (!hhmm) return ""; const [h, m] = hhmm.split(":").map(Number); return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };

function niceDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric"});
}

const when = (s, weekday) => new Date(s).toLocaleString("en-US", {timeZone: "America/New_York", ...(weekday ? {weekday: "short"} : {}), month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});

function ago(s) {
  const m = Math.round((Date.now() - new Date(s)) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} hr ago` : `${Math.round(h / 24)} days ago`;
}

// Google Flights' `tfs` parameter, for search pages and booking pages. legs: [{date, from, to,
// flight ("B6306": one exact flight, booking page), stops, airlines, via, timeFrom, timeTo}];
// two legs = a round trip. Same encoding as google_flights.build_tfs in the tracker.
function flightsUrl(page, legs, {adults, carry, checked}) {
  const bytes = [];
  const varint = (n, out) => { while (n > 127) { out.push((n & 127) | 128); n >>>= 7; } out.push(n); };
  const field = (num, data, out) => { varint(num << 3 | 2, out); varint(data.length, out); out.push(...data); };
  const int = (num, v, out) => { varint(num << 3, out); varint(v, out); };
  const str = x => [...new TextEncoder().encode(x)];
  for (const l of legs) {
    const d = [];
    field(2, str(l.date), d);
    if (l.flight) {
      const seg = [];
      field(1, str(l.from), seg); field(2, str(l.date), seg); field(3, str(l.to), seg); field(5, str(l.flight.slice(0, 2)), seg); field(6, str(l.flight.slice(2)), seg);
      field(4, seg, d);
    }
    const a = []; field(2, str(l.from), a); field(13, a, d);
    const b = []; field(2, str(l.to), b); field(14, b, d);
    int(5, l.stops || 0, d);
    for (const code of l.airlines || []) field(6, str(code), d);
    for (const code of l.via || []) field(15, str(code), d);
    if (l.timeFrom > 0) int(8, l.timeFrom, d);
    if (l.timeTo < 24) int(9, l.timeTo, d);
    field(3, d, bytes);
  }
  for (let i = 0; i < (adults || 1); i++) int(8, 1, bytes);
  int(9, 1, bytes);
  if (carry || checked) { const bg = []; if (carry) int(2, carry, bg); if (checked) int(3, checked, bg); field(13, bg, bytes); }
  int(19, legs.length > 1 ? 1 : 2, bytes);
  return `https://www.google.com/travel/flights/${page}?tfs=${encodeURIComponent(btoa(String.fromCharCode(...bytes)))}&hl=en&curr=USD`;
}
const FLIGHT_NO = /^[A-Z0-9]{2}\d+$/;

// Google Flights' booking page for a round trip on two exact flights: one ticket, so both must be
// on the same airline and the return must reverse the route. legs: [{from, to, date, flight}].
function roundTripBookLink(legs, adults, carry, checked) {
  if (!legs.every(l => FLIGHT_NO.test(l.flight || ""))) return null;
  return flightsUrl("booking", legs, {adults, carry, checked});
}

// Checkbox dropdown for several airports (user: pick more than one). picked = [] means all.
// It stays open while boxes are ticked (the page redraws) and closes on a tap outside.
let openMsel = null;
// Separate From and To pickers (user): ap.from / ap.to for the fare tables, ap.rtFrom / ap.rtTo
// for the round-trip box. In the box, From = where Going leaves and Coming back lands.
const ap = {from: [], to: [], rtFrom: [], rtTo: []};
function mselHtml(id, codes, picked, names = {}, prefix = "") {
  const label = !picked.length ? "All airports" : picked.length <= 2 ? picked.join(", ") : `${picked.length} airports`;
  return `<details class="msel" data-msel="${id}"${openMsel === id ? " open" : ""}><summary>${prefix ? `${prefix}: ` : ""}${esc(label)}</summary><div class="mselbox">
    <label><input type="checkbox" data-msel-all="${id}"${picked.length ? "" : " checked"}> All airports</label>
    ${codes.map(c => `<label><input type="checkbox" data-msel-pick="${id}" value="${esc(c)}"${picked.includes(c) ? " checked" : ""}> <b>${esc(c)}</b>${names[c] ? ` <span class="muted small">${esc(names[c])}</span>` : ""}</label>`).join("")}
    <div style="text-align:right;margin-top:4px"><button type="button" class="btn small" data-msel-done="1">Done</button></div></div></details>`;
}
const mselToggle = (list, code, on) => on ? [...new Set([...list, code])] : list.filter(c => c !== code);
const apOk = (list, code) => !list.length || list.includes(code);
const closeMsel = () => { openMsel = null; document.querySelectorAll("details.msel[open]").forEach(d => { d.open = false; }); };
document.addEventListener("click", e => {
  if (e.target.closest("[data-msel-done]")) { closeMsel(); return; }
  const s = e.target.closest(".msel > summary");
  if (s) {  // opening one list closes any other
    const d = s.parentElement;
    if (!d.open) document.querySelectorAll("details.msel[open]").forEach(x => { if (x !== d) x.open = false; });
    openMsel = d.open ? null : d.dataset.msel; return;
  }
  if (openMsel && !e.target.closest(".msel")) closeMsel();
});

// Cheapest way to fly a trip, counting the rental car (user: the car can turn a good fare into a
// bad deal and back). Each candidate: {dep, from, to, price (per person), pax, ...}. The car is
// picked up where Going lands and returned where Coming back leaves, one day per date between
// them (at least 1); trip.car = {day, at: {AIRPORT: rate}, oneway}. Returns the cheapest pair.
function planTrip(outs, backs, car, pairOk) {
  const rate = code => +(car?.at?.[code] ?? car?.day ?? 0) || 0;
  const days = (a, b) => Math.max(1, Math.round((Date.parse(b) - Date.parse(a)) / 864e5));
  const carCost = (o, b) => {
    if (!b || !rate(o.to)) return null;
    const d = days(o.dep, b.dep);
    return {days: d, rate: rate(o.to), oneway: o.to !== b.from ? (+car?.oneway || 0) : 0,
            total: d * rate(o.to) + (o.to !== b.from ? (+car?.oneway || 0) : 0), at: o.to, returnAt: b.from};
  };
  let best = null;
  for (const o of outs) {
    for (const b of backs.length ? backs : [null]) {
      if (b && b.dep < o.dep) continue;
      if (pairOk && !pairOk(o, b)) continue;
      const flights = o.price * o.pax + (b ? b.price * b.pax : 0), c = carCost(o, b);
      const total = flights + (c?.total || 0);
      if (!best || total < best.total) best = {out: o, back: b, flights, car: c, total};
    }
  }
  return best;
}

const money = n => "$" + Math.round(n).toLocaleString("en-US");
function planSummary(p) {
  const pax = p.out.pax === (p.back?.pax ?? p.out.pax) ? p.out.pax : null;
  const perPerson = pax ? ` (${money(p.out.price + (p.back?.price || 0))} per person × ${pax})` : "";
  const carText = p.car ? ` + car ${p.car.days} day${p.car.days > 1 ? "s" : ""} × ${money(p.car.rate)} at ${p.car.at}${p.car.oneway ? ` + ${money(p.car.oneway)} to return it at ${p.car.returnAt}` : ""} = ${money(p.car.total)}` : "";
  return `Flights ${money(p.flights)}${perPerson}${carText}`;
}
