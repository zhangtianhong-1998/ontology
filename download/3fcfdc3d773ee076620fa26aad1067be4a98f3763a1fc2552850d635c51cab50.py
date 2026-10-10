"""Report business relation coverage without treating connectivity as truth."""
from collections import Counter


def business_graph_coverage(plan):
    nodes = {item.id: item for item in plan.object_types if item.category == 'business_type'}
    neighbors = {key: set() for key in nodes}
    directions = Counter()

    def root(key):
        seen = set()
        while key in nodes and key not in seen:
            seen.add(key)
            key = nodes[key].parent
        return key

    count = 0
    for relation in plan.relation_types:
        if relation.category != 'business_relation_type':
            continue
        for left in relation.domain:
            for right in relation.range:
                if left in nodes and right in nodes:
                    neighbors[left].add(right)
                    neighbors[right].add(left)
                    directions[root(left) + ' -> ' + root(right)] += 1
                    count += 1
    unseen, components = set(nodes), []
    while unseen:
        queue, component = [min(unseen)], set()
        while queue:
            key = queue.pop()
            if key in component:
                continue
            component.add(key)
            queue.extend(neighbors[key] - component)
        unseen -= component
        components.append(sorted(component))
    return {'scope': 'accepted_business_types_and_relations_excluding_inheritance',
            'node_count': len(nodes), 'edge_count': count,
            'weak_component_count': len(components),
            'components': sorted(components, key=lambda values: (-len(values), values)),
            'isolated_types': [{'id': key, 'label': nodes[key].label, 'root': root(key)}
                               for key in sorted(nodes) if not neighbors[key]],
            'root_pair_counts': dict(sorted(directions.items())),
            'semantic_quality': 'connectivity_is_not_semantic_correctness'}
