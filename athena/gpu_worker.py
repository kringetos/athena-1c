"""GPU runtime probe and private JSON-line inference worker."""
import contextlib
import json
import sys

def probe():
    import numpy as np
    import onnx
    from onnx import helper,TensorProto
    import onnxruntime as ort
    ort.preload_dlls(directory="")
    node=helper.make_node('MatMul',['a','b'],['c'])
    graph=helper.make_graph([node],'gpu-test',[helper.make_tensor_value_info(n,TensorProto.FLOAT,[32,32]) for n in ['a','b']],
        [helper.make_tensor_value_info('c',TensorProto.FLOAT,[32,32])])
    model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',17)]);model.ir_version=8
    options=ort.SessionOptions();options.add_session_config_entry('session.disable_cpu_ep_fallback','1')
    session=ort.InferenceSession(model.SerializeToString(),options,providers=['CUDAExecutionProvider'])
    if 'CUDAExecutionProvider' not in session.get_providers():raise ValueError('CUDA не запустилась. Проверьте драйвер NVIDIA.')
    ones=np.ones((32,32),dtype=np.float32)
    np.testing.assert_allclose(session.run(None,{'a':ones,'b':ones})[0],32)
    print('GPU probe passed',flush=True)

def main():
    if '--probe' in sys.argv:probe();return
    # The parent uses UTF-8 JSON pipes; Windows otherwise defaults to cp1251.
    sys.stdin.reconfigure(encoding='utf-8', errors='strict')
    sys.stdout.reconfigure(encoding='utf-8', errors='strict')
    from athena.embeddings import LocalEmbeddings
    provider=None
    for line in sys.stdin:
        try:
            request=json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if provider is None:provider=LocalEmbeddings(request['models'],request['config'])
                vectors=provider._embed(request['texts'],request['prefix'])
                response={'vectors':[v.tolist() for v in vectors]}
        except Exception as exc:response={'error':str(exc)}
        print(json.dumps(response,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
