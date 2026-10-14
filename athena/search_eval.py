"""Reproducible retrieval evaluation: python -m athena.search_eval."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
from .catalog import Catalog
from .knowledge import Knowledge

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / 'tests/fixtures/search/dataset.json'


def evaluate(store, snapshot, cases, k=3, provider=None, mode='lexical'):
    rows = []
    for case in cases:
        expected = set(case['expected'])
        results=(store.hybrid_search(snapshot,case['query'],provider) if mode=='hybrid' else
                 store.semantic_search(snapshot,case['query'],provider) if mode=='semantic' else
                 store.search(snapshot,case['query'],limit=30))
        names = [row['name'] for row in results]
        ranks = [i + 1 for i, name in enumerate(names) if name in expected]
        relevant = set(names[:k]) & expected
        rows.append({**case, 'top_results': names[:k], 'similarities':[r.get('similarity') for r in results[:k]],
                     'reciprocal_rank': 1 / ranks[0] if ranks else 0,
                     'recall_at_k': len(relevant) / len(expected) if expected else None,
                     'passed': bool(relevant) if expected else not names})
    positive = [r for r in rows if r['expected']]
    negative = [r for r in rows if not r['expected']]
    return {'parser': store.status(snapshot)['parser'], 'mode':mode, 'k': k, 'cases': len(rows),
            'positive_cases': len(positive), 'negative_cases': len(negative),
            'hit_at_k': sum(r['passed'] for r in positive) / len(positive) if positive else None,
            'mean_recall_at_k': sum(r['recall_at_k'] for r in positive) / len(positive) if positive else None,
            'mrr': sum(r['reciprocal_rank'] for r in positive) / len(positive) if positive else None,
            'negative_accuracy': sum(r['passed'] for r in negative) / len(negative) if negative else None,
            'results': rows}


def run(dataset_path=DEFAULT_DATASET, project=None, mode='lexical'):
    dataset_path = Path(dataset_path)
    dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='athena-search-eval-') as tmp:
        directory = Path(tmp)
        if project:
            source = Path(project)
        else:
            source = directory / 'project'
            shutil.copytree(ROOT / 'examples/demo', source)
            for target, fixture in dataset.get('sources', {}).items():
                destination = (source / target).resolve()
                destination.relative_to(source.resolve())
                shutil.copyfile(dataset_path.parent / fixture, destination)
        store = Knowledge(directory / 'knowledge.sqlite3')
        snapshot = store.index(Catalog(source).scan(), 'evaluation')
        provider=None
        if mode!='lexical':
            from .embeddings import LocalEmbeddings
            provider=LocalEmbeddings(ROOT/'.athena/models')
            store.index_embeddings(snapshot,provider)
        result = evaluate(store, snapshot, dataset['cases'],provider=provider,mode=mode)
        result['description'] = dataset.get('description', '')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--project', type=Path, help='Read an existing export instead of the fixture')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--mode',choices=['lexical','semantic','hybrid'],default='lexical')
    args = parser.parse_args()
    result = run(args.dataset, args.project,args.mode)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in {'results', 'description'}}, indent=2))
    for row in result['results']:
        if not row['passed']:
            print('MISS:', row['query'])


if __name__ == '__main__':
    main()
