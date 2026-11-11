#!/usr/bin/env node
// Esporta la mappa coropletica come SVG autonomo (solo mappa, colori fissi, sfondo trasparente).
//   node export_svg.js                -> exports/mappa_scuole.svg        (tema chiaro)
//   node export_svg.js --dark         -> exports/mappa_scuole_scuro.svg  (tema scuro)
//   node export_svg.js --legend       -> exports/mappa_scuole_legenda.svg (con legenda; combinabile con --dark)
// Usa data/scuole.json (generato da build_data.py) e le librerie in vendor/.
const fs = require("fs");
const path = require("path");
const d3 = require("./vendor/d3.min.js");
const topojson = require("./vendor/topojson-client.min.js");

const args = process.argv.slice(2);
const dark = args.includes("--dark");
const legend = args.includes("--legend");

const data = JSON.parse(fs.readFileSync(path.join(__dirname, "data/scuole.json"), "utf8"));
const topo = JSON.parse(fs.readFileSync(path.join(__dirname, "data/province.topojson"), "utf8"));

const PAL = dark
  ? { ramp: ["#2e5f93", "#4580bd", "#67a1d6", "#97c0e8", "#cfe3f6"], empty: "#2a2f36", stroke: "#14171c", text: "#e8eaed" }
  : { ramp: ["#8db9e2", "#5e9bd3", "#3478bb", "#1b5596", "#0b3263"], empty: "#e6e6e6", stroke: "#ffffff", text: "#1a1a1a" };

const breaks = data.meta.breaks; // limiti inferiori delle classi 2..n
const labels = [1, ...breaks].map((lo, i, a) =>
  i === a.length - 1 ? `${lo} e più` : lo === a[i + 1] - 1 ? `${lo}` : `${lo}–${a[i + 1] - 1}`);
const count = {};
data.province.forEach((p) => (count[p.sigla] = p.n));
const classOf = (n) => (n ? breaks.filter((b) => n >= b).length : -1);
const nClasses = breaks.length + 1;
const color = (n) => {
  const k = classOf(n);
  if (k < 0) return PAL.empty;
  return PAL.ramp[nClasses >= 5 ? k : Math.round((k * 4) / Math.max(1, nClasses - 1))];
};

const fc = topojson.feature(topo, topo.objects[Object.keys(topo.objects)[0]]);
const W = 600;
const proj = d3.geoConicConformal().parallels([38, 46]).rotate([-12.5, 0]).fitWidth(W - 8, fc);
const path_ = d3.geoPath(proj);
const b = path_.bounds(fc);
const mapH = Math.ceil(b[1][1] + 4);

const r1 = (s) => s.replace(/(\d+\.\d{2})\d+/g, "$1"); // 2 decimali bastano
let out = "";
const H = legend ? mapH + 40 : mapH;
out += `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Mappa delle province italiane per numero di scuole con allarme bomba l'11 novembre 2026">\n`;
out += `<g id="province" stroke="${PAL.stroke}" stroke-width="0.6" stroke-linejoin="round">\n`;
fc.features.forEach((f) => {
  const s = f.properties.prov_acr;
  out += `<path id="${s}" fill="${color(count[s] || 0)}" d="${r1(path_(f))}"/>\n`;
});
out += `</g>\n`;
if (legend) {
  const items = [["Nessuna", PAL.empty], ...labels.map((l, i) => [l, PAL.ramp[nClasses >= 5 ? i : Math.round((i * 4) / Math.max(1, nClasses - 1))]])];
  let x = 8;
  out += `<g id="legenda" font-family="Inter, Arial, sans-serif" font-size="12" fill="${PAL.text}">\n`;
  items.forEach(([l, c]) => {
    out += `<rect x="${x}" y="${mapH + 12}" width="14" height="14" rx="3" fill="${c}"/><text x="${x + 20}" y="${mapH + 23}">${l}</text>\n`;
    x += 20 + l.length * 7 + 16;
  });
  out += `</g>\n`;
}
out += `</svg>\n`;

fs.mkdirSync(path.join(__dirname, "exports"), { recursive: true });
const file = path.join(__dirname, "exports", "mappa_scuole" + (dark ? "_scuro" : "") + (legend ? "_legenda" : "") + ".svg");
fs.writeFileSync(file, out);
console.log(`${path.relative(__dirname, file)}  ${(out.length / 1024).toFixed(0)} KB  (${fc.features.length} province, ${data.meta.total} scuole, classi: ${labels.join(", ")})`);
