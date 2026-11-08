# Lead / backing vocal extension, protocol v1

This extends existing projects without rewriting originals. New stem_role values: lead_vocal and backing_vocal. They mean learned lead/backing groups, not identified individual singers. Existing vocal is the combined vocal input.

Project.voice_parts optional object keyed by lead_vocal/backing_vocal:
{source_artifact_id, mode:"convert"|"original"|"mute", parameters:{model_id?,pitch_algorithm:"rmvpe",transpose:0,index_rate:0,protect:0.33},pitch_edits:[],notes:[],selected_candidate_id:null|id}
Project.parameters.active_voice_part is only editor focus; default vocal. Global pitch_edits/notes/parameters continue to apply to the old single vocal flow. PATCH voice_parts is a delta: merge only supplied parts/fields, deep-merge parameters, replace pitch_edits/notes arrays only when supplied. Omitted selected_candidate_id and other part remain unchanged; explicit null clears a selection. UI sends only touched per-part fields, including after revision conflict retry.

New job operation separate_harmony: input_artifact_id optional (defaults latest vocal; never automatically assumes original is dry vocals). parameters.harmony_model_id optional. Calls separation worker with parameters.separation_task="lead_backing" and selected model. Outputs lead_vocal/backing_vocal with input source and model/config hashes, real sample lengths, original offsets, warnings. A successful pair is committed to voice_parts together, source combined vocal muted for mixing, accompaniment retained. Failure keeps all existing edits and tracks.

New job operation convert_parts: optional parameters.parts:["lead_vocal","backing_vocal"] defaults both. Each part snapshots its own source, mode, model, pitch algorithm, transpose, edits and notes. Processing serialized; independent F0 analyses and model calls, independent candidates. Already committed part survives cancellation/failure in other part. parameters.mix_after=true optionally creates final mix after both. Do not auto-export. No cross-part candidate selection or editing overrides.

Existing pitch/convert job accepts parameters.voice_part:"lead_vocal"|"backing_vocal" and uses bound source/settings. Renders of one part only replace/mute corresponding source/previous candidate, preserve other part and accompaniment. voice_part overrides unrelated global edits/notes even if empty. Pitch artifacts, converted artifacts, candidates and converted tracks carry voice_part. Converted artifact stem_role remains converted so existing export provenance works.

Candidate selection for voice part is PATCH voice_parts[part].selected_candidate_id; backend validates source and candidate part, toggles only corresponding tracks. Single-vocal selected_candidate_id remains legacy. Mode original unmutes only bound raw part; convert chooses selected candidate if available; mute silences only its group. Mixing must never double-count original mixture or combined vocals with lead/backing groups.

Mix control PATCH accepts track_updates:{trackId:{gain_db?,pan?,mute?,solo?,offset_ticks?,effects?,name?}}. It merges only touched values and preserves tracks committed in the background. Track source/part identities cannot be changed through this delta.

Import APIs support roles lead_vocal/backing_vocal (explicit dry stems) and populate bindings. Manual split stems need no separation model. UI stage 人声整理 adds second-step separation and three-way source/lead/backing audition; voices stage adds active part selector, independent models/settings/mode and “分别转换两声部” action. Pitch canvas edits follow active part; candidates and A/B filter by part.

Capabilities.separation adds lead_backing_separation actual boolean and harmony_models metadata; config separation.models include task:"vocals_instrumental" or "lead_backing", explicit native_stem_roles mapping, model/config hashes and source/rights records. resource config separation.default_harmony_model. A model labelled Karaoke cannot be assumed to output isolated backing vocals from full original; this route is for isolated combined vocals. Record residual accompaniment/crosstalk warning and user listening review.
