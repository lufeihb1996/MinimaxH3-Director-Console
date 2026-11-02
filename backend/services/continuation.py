"""Pinned continuation frames and immutable queued generation inputs. No GPU work."""
import asyncio
import hashlib
import json
import math
import shutil
import subprocess
import uuid
import httpx

recommend_jobs = {}
from pathlib import Path
from fastapi import HTTPException, Form
from fastapi.responses import FileResponse

from backend.models.adapters.video_b15 import find_pass1_video, find_scene_mother_refs
from backend.models.video import video_adapter, video_model


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def source_info(shot, ns):
    prev=ns['previous_shot'](shot)
    if not prev: raise HTTPException(400,'没有上一镜，请使用 CUT/T2V 或手动指定首帧')
    source=find_pass1_video(prev) or ns['preferred_shot_video'](prev)
    if source is None: raise HTTPException(400,'上一镜没有可用视频，请先生成')
    return prev,source


def make_candidates(shot, ns, center=None):
    prev,source=source_info(shot,ns)
    duration=ns['media_duration_seconds'](source)
    fps=ns['_rate_to_float'](ns['media_video_info'](source).get('avg_frame_rate')) or 24
    if duration<=0: raise HTTPException(400,'无法读取视频时长，请检查 FFprobe')
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg: raise HTTPException(400,'FFmpeg not found')
    end=max(0,duration-1/fps)
    if center is not None:
        if not math.isfinite(center) or center<0 or center>duration: raise HTTPException(400,'时间超出视频范围')
        start=end=min(center,end)
    else: start=max(0,end-9/fps)
    source_hash=digest(source)
    batch=ns['ensure_shot_dir'](shot)/'continuity'/uuid.uuid4().hex
    batch.mkdir(parents=True)
    candidates=[]
    for i in range(1 if center is not None else 4):
        t=start if center is not None else start+(end-start)*i/3
        t=round(t*fps)/fps
        t=min(t,end)
        out=batch/f'{i}.png'
        p=subprocess.run([ffmpeg,'-v','error','-y','-i',str(source),'-ss',str(t),'-frames:v','1',str(out)],capture_output=True,timeout=90)
        if p.returncode or not out.is_file(): continue
        # Lightweight image quality ranking; deliberately not a character identity score.
        quality=None; usable=None
        try:
            from PIL import Image,ImageFilter,ImageStat
            im=Image.open(out).convert('L').resize((192,192))
            brightness=ImageStat.Stat(im).mean[0]
            sharpness=ImageStat.Stat(im.filter(ImageFilter.FIND_EDGES)).var[0]
            usable=12 <= brightness <= 245 and sharpness >= 15
            quality=round(sharpness,2)
        except ImportError: pass
        candidates.append({'index':i,'time':round(t,6),'hash':digest(out),'quality':quality,'usable':usable})
    if not candidates: raise HTTPException(400,'提取接续帧失败')
    manifest={'batch':batch.name,'source_shot_id':prev['id'],'source_path':str(source),'source_hash':source_hash,'duration':duration,'fps':fps,'candidates':candidates}
    (batch/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    for c in candidates: c['url']=f"/api/shots/{shot['id']}/continuation/candidate/{batch.name}/{c['index']}"
    ranked=[c for c in candidates if c['quality'] is not None and c['usable']]
    manifest['recommended']=None
    return manifest


def load_candidate(shot,ns,batch,index):
    if len(batch)!=32 or any(c not in '0123456789abcdef' for c in batch): raise HTTPException(400,'候选批次无效')
    folder=ns['ensure_shot_dir'](shot)/'continuity'/batch
    if not (folder/'manifest.json').is_file(): raise HTTPException(404,'Candidate frame not found')
    meta=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    entry=next((c for c in meta['candidates'] if c['index']==index),None)
    if entry is None: raise HTTPException(404,'Candidate frame not found')
    return folder/f'{index}.png',meta,entry


def is_auto_continue(shot):
    if str(shot.get('mode', '')).upper() != 'CONTINUE': return False
    meta = json.loads(shot.get('continuation_selection') or '{}')
    return meta.get('mode') == 'auto' or not shot.get('first_frame_path')


def auto_previous(shot, ns):
    prev = ns['previous_shot'](shot)
    if not prev or prev['scene_id'] != shot['scene_id']:
        raise HTTPException(400, '当前 Scene 没有上一镜，请使用 CUT/T2V 或手动指定首帧')
    return prev


def auto_wait_reason(shot, ns):
    source_id = shot.get('auto_source_shot_id')
    if not source_id: return ''
    try:
        prev = ns['get_shot'](int(source_id))
    except HTTPException:
        return '接续来源镜头已删除，请重新提交或手动选帧'
    label = f"Shot #{int(prev['shot_no']):03d}"
    if prev.get('error') or prev['status'] in {'FAILED', 'CANCELLED'}:
        return f'自动接续 {label} · 上一镜失败，等待重试成功'
    if prev['status'] != 'DONE':
        return f'自动接续 {label} · 等待上一镜完成'
    if ns['preferred_shot_video'](prev) is None:
        return f'自动接续 {label} · 上一镜视频不可用，等待重新生成'
    return ''


def resolve_auto_input(shot, ns):
    """Resolve only the deferred frame; retain queued parameters and templates."""
    if not shot.get('auto_source_shot_id'): return shot
    reason = auto_wait_reason(shot, ns)
    if reason: raise RuntimeError(reason)
    prev = auto_previous(shot, ns)
    if prev['id'] != shot['auto_source_shot_id']:
        raise RuntimeError('镜头顺序已变化，请重新提交自动接续')
    source = find_pass1_video(prev) or ns['preferred_shot_video'](prev)
    duration = ns['media_duration_seconds'](source)
    fps = ns['_rate_to_float'](ns['media_video_info'](source).get('avg_frame_rate')) or 24
    manifest = make_candidates(shot, ns, max(0, duration - 1 / fps))
    entry = manifest['candidates'][0]
    frame, _, _ = load_candidate(shot, ns, manifest['batch'], entry['index'])
    folder = Path(shot['queued_input_path']).parent
    dest = folder / 'auto_first_frame.png'
    shutil.copy2(frame, dest)
    meta = {k: v for k, v in manifest.items() if k not in {'candidates', 'recommended'}}
    meta.update(entry, mode='auto')
    shot = dict(shot, first_frame_path=str(dest), continuation_selection=json.dumps(meta, ensure_ascii=False))
    path = folder / 'input.json'
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['shot'] = {k: v for k, v in shot.items() if k != 'queued_input_path'}
    payload['hashes']['first_frame_path'] = digest(dest)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    ns['update_shot_state'](shot['id'], first_frame_path=str(dest), continuation_selection=shot['continuation_selection'])
    return shot


def freeze_inputs(shot,ns):
    two_pass_requested = bool(int(shot.get('second_pass_enabled') or 0))
    pass1_src = find_pass1_video(shot) if two_pass_requested else None
    if two_pass_requested and pass1_src is None and shot.get('shot_no') is not None:
        raise HTTPException(400, f"镜头 #{int(shot['shot_no']):03d} 尚未生成一采视频；请先切回「仅一采」生成视频，再开启二采做单视频精修")
    is_pass2_single = pass1_src is not None
    auto = False if is_pass2_single else is_auto_continue(shot)
    source_id = None
    if auto:
        source_id = auto_previous(shot, ns)['id']
    elif not is_pass2_single and shot['mode'].upper()=='CONTINUE':
        if not shot.get('first_frame_path') or not Path(shot['first_frame_path']).is_file():
            raise HTTPException(400,'手动首帧文件不存在，请重新选择或恢复自动接续')
        meta=json.loads(shot.get('continuation_selection') or '{}')
        if meta:
            _,source=source_info(shot,ns)
            if digest(source)!=meta['source_hash']: raise HTTPException(409,'上一镜视频已更新，请重新选帧')
    try:
        normalized = ns['normalize_video_parameters'](shot)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    frozen=dict(normalized); hashes={}
    if auto:
        frozen.update(auto_source_shot_id=source_id, first_frame_path='', continuation_selection='')
    settings=ns['get_settings']()
    profile=video_model(frozen.get('video_model'))
    adapter=video_adapter(profile['id'])
    try:
        frozen.update(adapter.snapshot_context(settings))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    frozen['video_profile']=profile
    frozen['lightx2v_profile']=adapter.frozen_profile(frozen, profile)
    folder=ns['ensure_shot_dir'](shot)/'inputs'/uuid.uuid4().hex
    folder.mkdir(parents=True)
    for name,template in adapter.snapshot_files(shot,profile,settings,ns['read_json_file']).items():
        (folder/name).write_text(json.dumps(template,ensure_ascii=False,indent=2),encoding='utf-8')
    frozen['input_fps']=int(settings.get('fps') or 24)
    if is_pass2_single and pass1_src and pass1_src.is_file():
        dest_v=folder/('pass1_source_video'+pass1_src.suffix)
        shutil.copy2(pass1_src,dest_v)
        frozen['pass1_source_video_path']=str(dest_v)
        hashes['pass1_source_video_path']=digest(dest_v)
        mother_refs=find_scene_mother_refs(shot) if shot['mode'].upper()!='T2V' else {}
        for field in ['ref1_path','ref2_path','ref3_path','ref4_path','first_frame_path']:
            frozen[field]=''
        raw_r1 = shot.get('ref1_path') or mother_refs.get('ref1_path') or ''
        if raw_r1 and Path(raw_r1).is_file():
            src_r1 = Path(raw_r1)
            dest_r1 = folder / ('ref1_path' + src_r1.suffix)
            shutil.copy2(src_r1, dest_r1)
            frozen['ref1_path'] = str(dest_r1)
            hashes['ref1_path'] = digest(dest_r1)
    else:
        for field in ['ref1_path','ref2_path','ref3_path','ref4_path','first_frame_path']:
            raw=shot.get(field)
            if auto and field == 'first_frame_path': continue
            if shot['mode'].upper()=='T2V' or (shot['mode'].upper()=='CONTINUE' and field not in {'ref1_path', 'first_frame_path'}): frozen[field]='';continue
            if field == 'ref1_path' and not raw and shot['mode'].upper() == 'CONTINUE':
                raw = find_scene_mother_refs(shot).get('ref1_path') or ''
            if raw:
                source=Path(raw)
                if not source.is_file(): raise HTTPException(400,f'参考图文件不存在：{field}')
                dest=folder/(field+source.suffix)
                shutil.copy2(source,dest);frozen[field]=str(dest);hashes[field]=digest(dest)
    try:
        song = ns['freeze_song_audio'](frozen, folder, ns) if ns.get('freeze_song_audio') else {}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    frozen.update(song)
    for field in ['native_vocals_path', 'native_mix_path']:
        if song.get(field): hashes[field] = digest(Path(song[field]))
    frozen.pop('queued_input_path',None)
    path=folder/'input.json'
    path.write_text(json.dumps({'shot':frozen,'hashes':hashes},ensure_ascii=False,indent=2),encoding='utf-8')
    return str(path)


def trim_for_continuity(row,source,ns):
    conn=ns['db']()
    following=conn.execute("SELECT * FROM shots WHERE UPPER(mode)='CONTINUE' AND status='DONE' AND (continuation_selection!='' OR completed_input_path!='') AND output_path!=''").fetchall();conn.close()
    matches=[]
    for item in following:
        meta=json.loads(item['continuation_selection'] or '{}')
        completed=item['completed_input_path']
        if completed and Path(completed).is_file():
            completed_shot=json.loads(Path(completed).read_text(encoding='utf-8'))['shot']
            meta=json.loads(completed_shot.get('continuation_selection') or '{}')
        if meta.get('source_shot_id')==row['id'] and meta.get('mode') != 'auto': matches.append(meta)
    if not matches:return source
    hashes=digest(source)
    matches=[m for m in matches if m['source_hash']==hashes]
    if not matches: raise RuntimeError('接续来源视频已改变，请重新选择接续帧并生成下一镜')
    times={round(m['time'],6) for m in matches}
    if len(times)>1: raise RuntimeError('同一来源镜头存在不同接续位置，请确认后再合并')
    end=times.pop()
    duration=ns['media_duration_seconds'](source)
    if duration-end<0.06:return source
    if float(row['target_duration'] or 0)>0 or int(row['lip_sync_enabled'] or 0):
        raise RuntimeError('接续点会截短歌曲/台词镜头，请重新选帧或改用 CUT')
    if end<=0:raise RuntimeError('接续点位于视频开头，无法保留上一镜，请重新选帧')
    target=ns['ensure_shot_dir'](ns['get_shot'](row['id']))/'continuity'/f'trim_{hashes[:16]}_{end:.6f}.mp4'
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:raise RuntimeError('FFmpeg not found')
        try:
            subprocess.run([ffmpeg,'-v','error','-y','-i',str(source),'-t',str(end),'-map','0:v:0','-map','0:a?','-c:v','libx264','-crf','18','-c:a','aac','-map_metadata','-1','-map_chapters','-1','-fflags','+bitexact','-flags:v','+bitexact','-flags:a','+bitexact','-movflags','+faststart',str(target)],check=True,timeout=300,capture_output=True)
        except subprocess.CalledProcessError as exc:
            target.unlink(missing_ok=True)
            detail = (exc.stderr or b'').decode('utf-8', errors='replace')[-2000:]
            raise RuntimeError(f'接续视频裁剪失败: {detail}') from exc
        except subprocess.TimeoutExpired as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError('接续视频裁剪超时，请重试') from exc
    return target


def install_routes(app,ns):
    @app.post('/api/shots/{shot_id}/continuation/auto')
    async def restore_auto(shot_id:int):
        shot = ns['get_shot'](shot_id)
        if shot['status'] in {'QUEUED','PREPARING','RUNNING'}:
            raise HTTPException(409, '生成期间不能更改接续模式')
        auto_previous(shot, ns)
        ns['update_shot_state'](shot_id, first_frame_path='', continuation_selection='')
        return ns['get_shot'](shot_id)

    @app.post('/api/shots/{shot_id}/continuation/candidates')
    async def candidates(shot_id:int, time:float|None=Form(None)):
        return await asyncio.to_thread(make_candidates,ns['get_shot'](shot_id),ns,time)

    @app.post('/api/shots/{shot_id}/continuation/recommend')
    async def recommend(shot_id:int,batch:str=Form(...)):
        shot=ns['get_shot'](shot_id)
        _,meta,_=load_candidate(shot,ns,batch,0)
        if len(meta['candidates'])!=4: raise HTTPException(400,'请先提取四张候选帧')
        _,source=source_info(shot,ns)
        if digest(source)!=meta['source_hash']: raise HTTPException(409,'上一镜视频已更新，请重新提取')
        files=[]
        for c in meta['candidates']:
            p,_,_=load_candidate(shot,ns,batch,c['index'])
            files.append(('files',(p.name,p.read_bytes(),'image/png')))
        try:
            async with httpx.AsyncClient(timeout=30,trust_env=False) as client:
                r=await client.post('http://127.0.0.1:3019/uploads',files=files,data={'tournament':'true','vision_tokens':'512'})
                if r.status_code!=200: raise HTTPException(r.status_code,r.json().get('detail','模型请求失败'))
                job=r.json()
        except httpx.HTTPError:
            raise HTTPException(503,'本机 imajev 服务未启动，请运行 D:/Imajev2/start-frames.ps1；仍可手动选帧')
        recommend_jobs[job['id']]={'shot_id':shot_id,'batch':batch,'indices':[c['index'] for c in meta['candidates']]}
        return {'id':job['id']}

    @app.get('/api/shots/{shot_id}/continuation/recommend/{job_id}')
    async def recommendation_status(shot_id:int,job_id:str):
        record=recommend_jobs.get(job_id)
        if not record or record['shot_id']!=shot_id: raise HTTPException(404,'推荐任务不存在')
        try:
            async with httpx.AsyncClient(timeout=10,trust_env=False) as client:
                r=await client.get('http://127.0.0.1:3019/jobs/'+job_id)
                r.raise_for_status();job=r.json()
        except httpx.HTTPError: raise HTTPException(503,'无法读取模型推荐进度，仍可手动选帧')
        recommendation=job.get('recommended')
        return {'status':job['status'],'stage':job['stage'],'completed':job['completed'],'total':3,
                'recommended':record['indices'][recommendation-1] if recommendation else None,
                'comparisons':job.get('comparisons',[]),'elapsed_seconds':job.get('elapsed_seconds')}

    @app.get('/api/shots/{shot_id}/continuation/candidate/{batch}/{index}')
    async def candidate(shot_id:int,batch:str,index:int):
        p,_,_=load_candidate(ns['get_shot'](shot_id),ns,batch,index)
        return FileResponse(p,media_type='image/png')

    @app.post('/api/shots/{shot_id}/continuation/select')
    async def select(shot_id:int,batch:str=Form(...),index:int=Form(...)):
        shot=ns['get_shot'](shot_id)
        if shot['status'] in {'QUEUED','PREPARING','RUNNING'}:raise HTTPException(409,'生成期间不能更改接续帧')
        p,meta,entry=load_candidate(shot,ns,batch,index)
        _,source=source_info(shot,ns)
        if digest(source)!=meta['source_hash']:raise HTTPException(409,'来源视频已更新，请重新提取')
        meta={k:v for k,v in meta.items() if k not in {'candidates','recommended'}};meta.update(entry)
        ns['update_shot_state'](shot_id,first_frame_path=str(p),continuation_selection=json.dumps(meta,ensure_ascii=False))
        return ns['get_shot'](shot_id)

    @app.post('/api/shots/{shot_id}/continuation/inherit-refs')
    async def inherit(shot_id:int):
        shot=ns['get_shot'](shot_id);prev=ns['previous_shot'](shot)
        if not prev:raise HTTPException(400,'没有上一镜')
        if shot['status'] in {'QUEUED','PREPARING','RUNNING'}:raise HTTPException(409,'生成期间不能更改参考图')
        values={}
        for i in range(1,5):
            field=f'ref{i}_path';raw=prev.get(field)
            if raw and Path(raw).is_file() and not shot.get(field):
                dest=ns['ensure_shot_dir'](shot)/'refs'/f'{uuid.uuid4().hex}_{i}{Path(raw).suffix}';dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(raw,dest);values[field]=str(dest)
        if not values:raise HTTPException(400,'上一镜没有可沿用图片，或当前参考位已填满')
        ns['update_shot_state'](shot_id,**values);return ns['get_shot'](shot_id)
