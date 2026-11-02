const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const ts = require("../../node_modules/typescript");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../src/timeline.ts"),
  "utf8",
);
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
  },
}).outputText;
const moduleValue = { exports: {} };
vm.runInNewContext(compiled, {
  module: moduleValue,
  exports: moduleValue.exports,
});
const { timelineRows, timelineVisibility } = moduleValue.exports;
const project = {
  assets: [],
  tracks: [],
  candidates: [],
  separation_runs: [],
  view_preferences: {},
};
function add(id, role, kind, length = 10) {
  const asset = {
    id,
    stem_role: role,
    processing_kind: kind,
    media_type: "audio/wav",
    sample_count: length * 48000,
    sample_rate: 48000,
    input_artifact_ids: kind === "decode" ? [] : ["source"],
  };
  const track = {
    id: "track_" + id,
    role,
    artifact_id: id,
    offset_ticks: 0,
    mute: false,
    gain_db: 0,
    effects: [],
  };
  project.assets.push(asset);
  project.tracks.push(track);
  return track;
}
const original = add("source", "original", "decode");
const importedStem = add("external", "backing_vocal", "decode");
const vocal = add("vocal", "vocal", "model_inference");
const lead = add("lead", "lead_vocal", "model_inference");
const preview = add("preview", "converted", "model_inference");
const converted = add("converted", "converted", "model_inference");
add("mix", "mix", "dsp", 40);
project.candidates.push(
  {
    id: "candidate_preview",
    artifact_id: preview.artifact_id,
    preview_only: true,
  },
  { id: "candidate_full", artifact_id: converted.artifact_id, selected: true },
);
const trackIds = (rows) => rows.map((row) => row.id).join(",");

assert.equal(
  trackIds(timelineRows(project, 0).visible),
  trackIds([original, importedStem]),
);
assert.equal(
  timelineRows(project, 0).duration,
  10,
  "later mix must not extend the import ruler",
);
assert.equal(
  trackIds(timelineRows(project, 1).visible),
  trackIds([original, importedStem, vocal, lead]),
);
assert.ok(
  timelineRows(project, 2).visible.some((row) => row.id === preview.id),
);
assert.ok(
  !timelineRows(project, 2).visible.some((row) => row.id === converted.id),
);
assert.ok(!timelineRows(project, 3).visible.some((row) => row.role === "mix"));

const audioState = JSON.stringify([
  project.assets,
  project.tracks,
  project.candidates,
]);
project.view_preferences = timelineVisibility({}, original.id, true);
assert.ok(
  !timelineRows(project, 0).visible.some((row) => row.id === original.id),
);
assert.ok(
  timelineRows(project, 0).archived.some((row) => row.id === original.id),
);
const persisted = JSON.parse(JSON.stringify(project));
assert.ok(
  !timelineRows(persisted, 0).visible.some((row) => row.id === original.id),
);
project.view_preferences = timelineVisibility(
  project.view_preferences,
  original.id,
  false,
);
assert.ok(
  timelineRows(project, 0).visible.some((row) => row.id === original.id),
);
project.view_preferences = timelineVisibility(
  project.view_preferences,
  converted.id,
  false,
);
assert.ok(
  !timelineRows(project, 0).visible.some((row) => row.id === converted.id),
  "restored downstream audio stays out of earlier stages",
);
assert.ok(
  !timelineRows(project, 1).archived.some((row) => row.id === converted.id),
);

const defaults = [lead];
assert.equal(
  trackIds(timelineRows(project, 1, defaults).visible),
  trackIds([original, lead]),
);
project.view_preferences = timelineVisibility(
  project.view_preferences,
  vocal.id,
  false,
);
assert.ok(
  timelineRows(project, 1, defaults).visible.some((row) => row.id === vocal.id),
);
project.view_preferences = timelineVisibility(
  project.view_preferences,
  converted.id,
  true,
);
assert.ok(
  !timelineRows(project, 4).visible.some((row) => row.id === converted.id),
  "adopted audio can be visually archived",
);
assert.equal(
  JSON.stringify([project.assets, project.tracks, project.candidates]),
  audioState,
);

console.log(
  JSON.stringify(
    {
      passed: true,
      checks: [
        "stage isolation",
        "stage duration",
        "imported stems",
        "pitch audition scope",
        "individual archive and restore",
        "refresh persistence",
        "downstream restore isolation",
        "historical track inclusion",
        "adopted track archive",
        "audio state preservation",
      ],
    },
    null,
    2,
  ),
);
