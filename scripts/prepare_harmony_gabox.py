"""Explicit, hash-pinned preparation of the alternative Karaoke V2 checkpoint."""
import argparse
import copy
from pathlib import Path
import prepare_harmony_model as base

FILES = [
    {'name': 'mel_band_roformer_karaoke_gabox_v2.ckpt', 'bytes': 913090472,
     'sha256': 'ec34be50327aeaf1a996c27977f5c30d1ac80c0076d69683d3e5184c31ea29d3', 'github_asset_id': 280998842},
    {'name': 'config_mel_band_roformer_karaoke_gabox.yaml', 'bytes': 1811,
     'sha256': '16b726891076fd0bebab9ce038240b51afb22b04eac6026a7f0356178dcb65b7', 'github_asset_id': 282884319},
]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if not args.download:
        parser.error('Use --download to explicitly prepare the 913 MB alternative model.')
    base.DESTINATION.mkdir(parents=True, exist_ok=True)
    records=base.RECORDS
    base.RECORDS=records/'gabox-v2'
    base.RECORDS.mkdir(parents=True, exist_ok=True)
    for entry in FILES:
        base.download(entry)
    manifest = copy.deepcopy(base.registry_manifest())
    manifest.update(id='melband-karaoke-gabox-v2', name='MelBand RoFormer Karaoke V2 · Gabox',
                    path=str(base.DESTINATION / FILES[0]['name']), config_path=str(base.DESTINATION / FILES[1]['name']),
                    model_hash=FILES[0]['sha256'], config_hash=FILES[1]['sha256'], authors=['Gabox'],
                    source_url=base.BASE + FILES[0]['name'], config_source_url=base.BASE + FILES[1]['name'],
                    source_asset_ids=[x['github_asset_id'] for x in FILES], test_status='download_verified_not_yet_rendered',
                    semantic_sources=['https://github.com/nomadkaraoke/python-audio-separator/blob/v0.44.2/audio_separator/models.json',
                                      'https://github.com/nomadkaraoke/python-audio-separator/blob/main/docs/deton24-model-mapping-and-ensemble-guide.md'],
                    architecture='MelBand RoFormer')
    manifest.pop('source_release_updated',None)
    existing=records/'gabox-v2-manifest.json'
    if existing.exists():
        old=base.read_json(existing)
        if old.get('model_hash')==manifest['model_hash'] and old.get('config_hash')==manifest['config_hash']:
            for key in ('test_status','verification','validation_scope'):
                if key in old:manifest[key]=old[key]
    base.write_json(existing, manifest)
    path = base.ROOT / 'engine-envs/resources.json'
    config = base.read_json(path)
    config['separation']['models'] = [m for m in config['separation']['models'] if m['id'] != manifest['id']] + [manifest]
    base.write_json(path, config)
    base.write_json(base.RECORDS/'download-state.json',{'state':'verified','model_id':manifest['id'],'sha256':manifest['model_hash']})
    print('Registered alternative: ' + manifest['id'], flush=True)

if __name__ == '__main__':
    main()
