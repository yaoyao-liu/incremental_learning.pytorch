import itertools

import numpy as np
import torch
from torch.nn import functional as F


def triplet_loss(
    features,
    targets,
    distance="l2",
    ranking=False,
    aggreg="mean",
    margin=0.2,
    factor=1.,
    normalize=False,
    sampling_config={"type": "random"}
):
    """Triplet loss, reducing distance between two similar samples & maximizing distances with a third
    dissimilar sample.

    References:
        * Deep metric learning using Triplet network
          Hoffer et al.
          2014
        * Deep Triplet Ranking Networks for One-Shot Recognition
          Meng et al.
          2018
        * Facenet: A unified embedding for face recognition and clustering
          Schroff et al.
          CVPR 2015.
        * (AdaMine) Cross-Modal Retrieval in the Cooking Context: Learning
          Semantic Text-Image Embeddings
          Carvalho et al.
          2018

    :param features: A batch of 1d features.
    :param targets: Sparse targets.
    :param distance: Distance to use.
    :param ranking: To use Triplet Ranking Loss instead of Triplet Loss.
    :param aggreg: Aggregation method for every triplets.
    :param margin: Margin to push negative far appart.
    :param factor: A float factor multiplied by the loss.
    :return: A float scalar loss.
    """
    if normalize:
        features = F.normalize(features, dim=1, p=2)

    if sampling_config["type"] == "random":
        anchor_indexes, pos_indexes, neg_indexes = _triplet_random_sampling(features, targets)
    elif sampling_config["type"] == "3third":
        assert len(features) % 3 == 0
        indexes = torch.arange(len(features))
        anchor_indexes = indexes[0::3]
        pos_indexes = indexes[1::3]
        neg_indexes = indexes[2::3]
    elif sampling_config["type"] == "facenet":
        anchor_indexes, pos_indexes, neg_indexes = _triplet_facenet_sampling(
            features, targets, sampling_config["semihard"], distance
        )
    else:
        raise ValueError("Unknown sampling {}.".format(sampling_config["type"]))

    anchor_features = features[anchor_indexes]
    pos_features = features[pos_indexes]
    neg_features = features[neg_indexes]

    ap_dist = _pair_distance(anchor_features, pos_features, distance_type=distance)
    an_dist = _pair_distance(anchor_features, neg_features, distance_type=distance)

    loss = _triplet(ap_dist, an_dist, margin, aggreg)

    if ranking:
        pn_dist = _pair_distance(pos_features, neg_features, distance_type=distance)
        loss += _triplet(ap_dist, pn_dist, margin, aggreg)

    return factor * loss


# -----------------
# Private functions
# -----------------


def _triplet(pos_distance, neg_distance, margin, aggreg="mean"):
    triplets = torch.clamp(margin + pos_distance - neg_distance, min=0.)

    if aggreg == "mean":
        return torch.mean(triplets)
    elif aggreg == "sum":
        return torch.sum(triplets)
    elif aggreg == "adamine":
        return torch.sum(triplets) / max(len(triplets[triplets > 0]), 1)

    raise ValueError("Unknown aggregation method for triplet: {}.".format(aggreg))


def _triplet_facenet_sampling(features, targets, semihard=True, distance="l2squared"):
    # Forgive me for this code...

    # Generate a distance matrix of shape (batch_size, batch_size).
    # The diagonal is obviously null.
    pairwise_distances = _dense_distance(features, distance_type=distance)

    anchor_indexes, positive_indexes, negative_indexes = [], [], []

    targets = targets.cpu().numpy()
    for target in set(targets.tolist()):
        indexes = np.where(targets == target)[0].tolist()
        neg_indexes = np.where(targets != target)[0].tolist()

        positive_pairs = list(itertools.combinations(indexes, 2))

        _anchors = torch.tensor([pair[0] for pair in positive_pairs])
        _positives = torch.tensor([pair[1] for pair in positive_pairs])
        if semihard:
            ap_dist = pairwise_distances[_anchors, _positives]

            nb_pos = len(indexes)
            nb_neg = len(targets) - nb_pos

            an_dist = pairwise_distances[torch.tensor(indexes).repeat_interleave(nb_neg, 0),
                                         torch.tensor(neg_indexes).repeat(1, nb_pos)[0]]

            anchors = []
            positives = []
            negatives = []
            for i in range(len(ap_dist)):
                if (ap_dist[i] < an_dist[i]).any():
                    negatives.append(
                        neg_indexes[(an_dist[i] == an_dist[i][ap_dist[i] < an_dist[i]].min()
                                    ).argmax().item()]
                    )

                    positives.append(_positives[i])
                    anchors.append(_anchors[i])
        else:
            negatives = np.random.choice(neg_indexes, size=len(_anchors), replace=False).tolist()
            anchors = _anchors.tolist()
            positives = _positives.tolist()

        assert len(negatives) == len(anchors) == len(positives)
        anchor_indexes.extend(anchors)
        positive_indexes.extend(positives)
        negative_indexes.extend(negatives)

    return torch.tensor(anchor_indexes), torch.tensor(positive_indexes), torch.tensor(negative_indexes)


def _triplet_random_sampling(features, targets):
    anchor_indexes, pos_indexes, neg_indexes = [], [], []
    targets = targets.cpu().numpy()

    for target in targets:
        target_indexes = np.where(target == targets)[0]

        poss = np.random.choice(target_indexes, size=2, replace=len(target_indexes) < 2)
        neg = np.random.choice(np.where(target != targets)[0], size=1)

        anchor_indexes.append(poss[0])
        pos_indexes.append(poss[1])
        neg_indexes.append(neg[0])

    assert len(anchor_indexes) == len(pos_indexes) == len(neg_indexes)

    anchor_indexes = torch.tensor(anchor_indexes)
    pos_indexes = torch.tensor(pos_indexes)
    neg_indexes = torch.tensor(neg_indexes)

    return anchor_indexes, pos_indexes, neg_indexes


def _dense_distance(features, distance_type="l2"):
    if distance_type == "l2":
        return torch.cdist(features, features, p=2)
    if distance_type == "l2squared":
        return torch.pow(torch.cdist(features, features, p=2), 2)
    elif distance_type == "l1":
        return torch.cdist(features, features, p=1)
    elif distance_type == "cosine":
        features_normalized = F.normalize(features)
        return torch.mm(features_normalized, features_normalized.t())

    raise ValueError("Unknown distance type {}.".format(distance_type))


def _pair_distance(a, b, distance_type="l2"):
    if distance_type == "l2":
        return F.pairwise_distance(a, b, p=2)
    if distance_type == "l2squared":
        return torch.pow(F.pairwise_distance(a, b, p=2), 2)
    elif distance_type == "l1":
        return F.pairwise_distance(a, b, p=1)
    elif distance_type == "cosine":
        return 1 - torch.cosine_similarity(a, b)

    raise ValueError("Unknown distance type {}.".format(distance_type))
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               return torch.pow(F.pairwise_distance(a, b, p=2), 2)
    elif distance_type == "l1":
        return F.pairwise_distance(a, b, p=1)
    elif distance_type == "cosine":
        return 1 - torch.cosine_similarity(a, b)

    raise ValueError("Unknown distance type {}.".format(distance_type))
