# SONORA local protocol v1

Canonical contract shared by UI, Python orchestrator and workers. Integer sample offsets, end-exclusive ranges. Project timeline uses 48000 ticks/sec. No mock inference.

Base http://127.0.0.1:8765/api/v1, Authorization: Bearer <session-token>. Startup launcher supplies token through URL fragment, UI removes fragment and keeps in sessionStorage. Tauri may supply via get_connection IPC. CORS only localhost:5173, 127.0.0.1:5173, tauri://localhost, http://tauri.localhost. API never scans ports.

GET /health -> {protocol_version:1, name:"SONORA", status:"ready"} (no auth; no private information)
GET /capabilities -> {engines:[{id,name,status,reason,capabilities,...}],device:{...},protocol_version:1}
GET /models -> {models:[{id,name,engine_id,model_hash,path,sample_rate,capabilities,license,test_status,...}]}
POST /models/import JSON {path} -> model; restrict to configured roots; copies/hardlinks immutable resource, safe probe only.
GET /projects -> {projects:[Project]}
POST /projects {name,task:"speech"|"cover"|"rewrite"} -> Project
GET /projects/{id} -> Project
PATCH /projects/{id} {revision,patch:{...editable keys...}} -> Project; stale revision 409
POST /projects/{id}/history {direction:"undo"|"redo",revision} -> Project
POST /projects/{id}/import multipart file + role (original|vocal|instrumental|guide), or POST /projects/{id}/import-path {path,role} -> {project:Project,artifact:Artifact}
POST /projects/{id}/demo {} -> {project,artifact} (explicitly synthetic technical fixture)
GET /projects/{id}/recipe -> JSON recipe
POST /projects/restore multipart file -> Project (references relink by hash; never arbitrary paths)
POST /projects/{id}/jobs {revision,idempotency_key,operation:"separate"|"pitch"|"convert"|"mix"|"export"|"pipeline",input_artifact_id?,parameters:{...}} -> Job
GET /jobs?project_id=... -> {jobs:[Job]}
GET /jobs/{id} -> Job
POST /jobs/{id}/cancel {} -> Job
POST /jobs/{id}/retry {} -> Job
GET /events?after=N -> {events:[{sequence,type,job_id,project_id,data,created_at}],last_sequence} replayable polling; optional SSE /events/stream
GET /artifacts/{id} -> Artifact
GET /artifacts/{id}/audio -> media with Range, auth; frontend fetches blob using bearer
GET /artifacts/{id}/waveform -> {sample_rate,sample_count,channels,peaks:[[min,max],...],levels:[...]}
GET /artifacts/{id}/data -> pitch JSON
GET /exports/{id}/download -> file, auth
POST /projects/{id}/midi multipart file+revision -> Project (PPQ MIDI type0/1)
GET /projects/{id}/midi -> standard MIDI reference notes
POST /projects/{id}/experiments {revision,idempotency_key,input_artifact_id?,range_ticks?,variants:[{transpose?,index_rate?,protect?,pitch_algorithm?,seed?}]} -> {jobs}; max8 serial variants with immutable shared source
POST /projects/{id}/relink {hash,path} -> {project,artifact}; requires exact original or artifact hash, restores saved track placement

Additional job operations: comp {base_candidate_id,segments:[{candidate_id,start_tick,end_tick,fade_ms:20}]} preserves the full base candidate timeline and blends override edges; export_stems {semantics:raw|post_fader_fx,format:wav|flac} aligns enabled stems to common 48kHz stereo start/length; svs {svs_model_id?,language:ja,notes:[{start_tick,end_tick,pitch_midi_float,lyric,phonemes?}],duration_ticks,steps:10} synthesizes real Japanese lyrics in a locked window (0.1–60s), no source audio required.

RVC range_ticks requests an actual contextual excerpt. Audio artifacts carry timeline_offset_ticks and source_sample_rate/source_offset. Transport must map global playhead to local audio time. A/B clamps to the shared interval. Excerpts do not promise sample equivalence to full-node render and cannot silently replace full-song candidates in full-length comps.

Project: {id,name,schema_version:1,revision,created_at,updated_at,task,timebase:48000,assets:Artifact[],tracks:Track[],selection:{start:number,end:number},playhead:number,mode:"quick"|"detail"|"expert",parameters:{model_id?,pitch_algorithm?,transpose?,index_rate?,protect?,...},pitch_edits:[],notes:[],lyrics:[],candidates:Candidate[],selected_candidate_id?,exports:[],graph:[],warnings:[]}. Selection/playhead are integer timeline ticks. Every saved change is revisioned. Track: {id,name,role,artifact_id,gain_db,pan,mute,solo,offset_ticks:0,effects:[]}. Candidate: {id,name,source_artifact_id,artifact_id,model_id,parameters,generation_depth,state,notes,selected,...}.

Artifact: {id,sha256,path,name,media_type,sample_rate,sample_count,channels,processing_kind:"demo"|"decode"|"dsp"|"model_inference",input_artifact_ids:[],job_id,worker,engine,model_hash,source_offset:0,frame_hop:0,frame_center_offset:0,valid_range:[0,sample_count],time_map:{source_rate,output_rate,source_offset,output_offset},commit_state:"committed",stem_role,...}. Engine/model hashes required for model_inference. Pitch stored separate JSON {sample_rate:16000,hop:160,frame_center_offset:0,f0_hz:(number|null)[],voiced:boolean[],native_score:[],score_kind,estimator,version,source_hash,locked_regions:[],...}.

Job: {id,project_id,project_revision,operation,state,progress:null|number,progress_basis:"nodes"|"unknown",stage,message,parameters,input_artifact_id,artifact_ids:[],created_at,updated_at,error:null|{code,message,recovery},attempts:1}. State queued|preparing|running|blocked|succeeded|failed|cancelled. Convert params model_id,pitch_algorithm,transpose,index_rate,protect,pitch_artifact_id?,pitch_edits?, range_ticks?,chain_candidate_id? (explicit only). Export params artifact_id?,format:wav|flac|mp3,name,selection_only,model_converted:boolean. Mix params tracks?:[{artifact_id,gain_db,pan,mute,solo,offset_ticks,effects}],sample_rate:48000.

## Worker disk protocol

`python workers/<engine>/run.py --request <json> --response <json>` in isolated subprocess. JSON request {protocol_version:1,job_id,input_path,output_dir,parameters,model?,pitch_path?,resource_config_path}. Response atomically written {ok:true,outputs:[{path,role,processing_kind,sample_rate,sample_count,channels,engine,model_hash?,requested_f0_hash?,consumed_f0_hash?,...}],metadata:{...},warnings:[]}, or {ok:false,error:{code,message}} and nonzero exit. stdout human logs; parent uses actual lifecycle progress and kills entire process tree on cancellation. Worker outputs must be under output_dir. Resource config lives engine-envs/resources.json, generated by scripts/bootstrap_resources.py. Each worker supports --probe (JSON environment/capabilities), no downloads at render time. Engines: separation, pitch, rvc. Root builds FFmpeg/DSP orchestration. Worker owner also owns resource bootstrap/registries and engine-envs. UI owner owns apps/web exclusively. Desktop owner apps/desktop and desktop launch scripts exclusively. Root owns services, contracts, integration tests, root package, README.
