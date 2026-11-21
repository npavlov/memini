import asyncio, json, math, os, resource, statistics, time
from dataclasses import dataclass
from pathlib import Path
from memini.config import load_settings
from memini.rerank.factory import local_reranker
from memini.search.engine import _rerank

@dataclass(frozen=True)
class CapturedHit:
    header: str
    snippet: str
    score: float = -1.0

async def main():
    settings = load_settings(dict(os.environ, MEMINI_DATABASE_URL='postgresql://unused/unused'))
    if os.getenv('OVERRIDE_MODEL'):
        import onnxruntime as ort
        original=ort.InferenceSession
        def from_file(path, *, sess_options, providers):
            import gzip, hashlib
            graph=gzip.decompress(Path('/bench/per-token.onnx.gz').read_bytes())
            assert hashlib.sha256(graph).hexdigest()=='7f8cf431d2c1a36b251357dbff046a2b28480e2ce5bf45b76c81def591f49111'
            sess_options.add_session_config_entry('session.model_external_initializers_file_folder_path',str(Path(path).parent))
            return original(graph,sess_options=sess_options,providers=providers)
        ort.InferenceSession=from_file
    rr = local_reranker(settings)
    start = time.monotonic()
    await rr.load()
    init = time.monotonic() - start
    rows = [json.loads(line) for line in Path('/in.jsonl').read_text().splitlines()][::3]
    runs=[]
    for row in rows:
        hits=[CapturedHit(*doc.split('\n',1)) for doc in row['documents']]
        start=time.monotonic()
        result=await _rerank(rr,row['query'],hits,3)
        item=dict(index=len(runs),mode=row['mode'],id=row['id'],s=time.monotonic()-start,fallback=result is hits,depth=len(hits))
        runs.append(item)
        print(json.dumps(item),flush=True)
    times=sorted(r['s'] for r in runs)
    print(json.dumps(dict(n=len(runs),threads=rr.threads,load_s=init,p50=statistics.median(times),p95=times[math.ceil(.95*len(times))-1],max=times[-1],fallbacks=sum(r['fallback'] for r in runs),rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,over_5=sum(t>5 for t in times),cpu_quota=Path('/sys/fs/cgroup/cpu.max').read_text().strip())),flush=True)
asyncio.run(main())
