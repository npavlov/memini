from pathlib import Path
from tokenizers import Tokenizer
from memini.rerank.factory import local_reranker
from memini.rerank import local
from memini.config import load_settings
import numpy as np, onnxruntime as ort, json, gzip
row=next(r for r in map(json.loads,open('/bench/inputs.jsonl')) if r['mode']=='dense' and r['id']=='doc-en-dues')
cfg=load_settings({'MEMINI_DATABASE_URL':'postgresql://unused/db','MEMINI_SEARCH_RERANKER_MODEL_DIR':'/opt/memini/models'})
model=local_reranker(cfg); tokenizer=Tokenizer.from_file(str(model.files_dir/'tokenizer.json'))
tokenizer.no_truncation();tokenizer.no_padding()
prefix=tokenizer.encode(local._PREFIX,add_special_tokens=False).ids
suffix=tokenizer.encode(local._SUFFIX,add_special_tokens=False).ids
tokenizer.encode_special_tokens=True
feeds=[]
for d in row['documents']:
 body=f"<Instruct>: {local.INSTRUCTION}\n<Query>: {row['query']}\n<Document>: {d}"
 ids=prefix+tokenizer.encode(body,add_special_tokens=False).ids[:1024-len(prefix)-len(suffix)]+suffix
 feeds.append({'input_ids':np.array([ids],dtype='int64'),'attention_mask':np.ones((1,len(ids)),dtype='int64')})
for name in ['per-token','precise','unsigned','blockwise']:
 options=local.session_options(ort,4)
 if name == 'precise': options.add_session_config_entry('session.x64quantprecision','1')
 options.add_session_config_entry('session.model_external_initializers_file_folder_path',str(model.files_dir/'onnx'))
 ses=ort.InferenceSession(gzip.decompress((Path('/bench')/(('per-token' if name=='precise' else name)+'.onnx.gz')).read_bytes()),sess_options=options,providers=['CPUExecutionProvider'])
 logits=[ses.run(None,f)[0][0].tolist() for f in feeds]; sc=[b-a for a,b in logits]
 print(json.dumps([name,sc,sorted(range(3),key=sc.__getitem__,reverse=True)]),flush=True)
 del ses
