import json,gzip,time,sys
from pathlib import Path
import numpy as np, onnxruntime as ort
from tokenizers import Tokenizer
from memini.config import load_settings
from memini.rerank import local
from memini.rerank.factory import local_reranker
cfg=load_settings({'MEMINI_DATABASE_URL':'postgresql://unused/db','MEMINI_SEARCH_RERANKER_MODEL_DIR':'/opt/memini/models'})
model=local_reranker(cfg);tok=Tokenizer.from_file(str(model.files_dir/'tokenizer.json'))
tok.no_truncation();tok.no_padding();prefix=tok.encode(local._PREFIX,add_special_tokens=False).ids;suffix=tok.encode(local._SUFFIX,add_special_tokens=False).ids;tok.encode_special_tokens=True
cases=json.loads(Path('/bench/screen.json').read_text())
for case in cases:
 feeds=[]
 for doc in case['documents']:
  text=f"<Instruct>: {local.INSTRUCTION}\n<Query>: {case['query']}\n<Document>: {doc}"
  ids=prefix+tok.encode(text,add_special_tokens=False).ids[:1024-len(prefix)-len(suffix)]+suffix
  feeds.append({'input_ids':np.array([ids],dtype=np.int64),'attention_mask':np.ones((1,len(ids)),dtype=np.int64)})
 case['feeds']=feeds
for name in sys.argv[1:]:
 o=local.session_options(ort,4)
 o.add_session_config_entry('session.model_external_initializers_file_folder_path',str(model.files_dir/'onnx'))
 sess=ort.InferenceSession(gzip.decompress(Path('/bench',name+'.onnx.gz').read_bytes()),sess_options=o,providers=['CPUExecutionProvider'])
 for case in cases:
  t=time.monotonic(); scores=[]
  for feed in case['feeds']:
   no,yes=sess.run(None,feed)[0][0];scores.append(float(yes-no))
  order=sorted(range(len(scores)),key=lambda i:-scores[i]);rank=next((i+1 for i,j in enumerate(order) if case['matches'][j]),None)
  print(json.dumps(dict(graph=name,case=case['id'],rank=rank,seconds=round(time.monotonic()-t,3),scores=scores)),flush=True)
 del sess
