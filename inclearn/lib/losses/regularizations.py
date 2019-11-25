import numpy as np
import torch
from torch.nn import functional as F

from inclearn.lib import utils


def weights_orthogonality(weights, margin=0.):
    """Regularization forcing the weights to be disimilar.

    :param weights: Learned parameters of shape (n_classes, n_features).
    :param margin: Margin to force even more the orthogonality.
    :return: A float scalar loss.
    """
    normalized_weights = F.normalize(weights, dim=1, p=2)
    similarities = torch.mm(normalized_weights, normalized_weights.t())

    # We are ignoring the diagonal made of identity similarities:
    similarities = similarities[torch.eye(similarities.shape[0]) == 0]

    return torch.mean(F.relu(similarities + margin))


def ortho_reg(weights, config):
    """Regularization forcing the weights to be orthogonal without removing negative
    correlation.

    Reference:
        * Regularizing CNNs with Locally Constrained Decorrelations
          Pau et al.
          ICLR 2017

    :param weights: Learned parameters of shape (n_classes, n_features).
    :return: A float scalar loss.
    """
    normalized_weights = F.normalize(weights, dim=1, p=2)
    similarities = torch.mm(normalized_weights, normalized_weights.t())

    # We are ignoring the diagonal made of identity similarities:
    similarities = similarities[torch.eye(similarities.shape[0]) == 0]

    x = config.get("lambda", 10.) * (similarities - 1.)

    return config.get("factor", 1.) * torch.log(1 + torch.exp(x)).sum()


def global_orthogonal_regularization(
    features, targets, new_classes_index, factor=1., normalize=False, sampling="per_target"
):
    """Global Orthogonal Regularization (GOR) forces features of different
    classes to be orthogonal.

    # Reference:
        * Learning Spread-out Local Feature Descriptors.
          Zhang et al.
          ICCV 2016.

    :param features: A flattened extracted features.
    :param targets: Sparse targets.
    :return: A float scalar loss.
    """
    if normalize:
        features = F.normalize(features, dim=1, p=2)

    positive_indexes, negative_indexes = [], []
    targets = targets.cpu().numpy()
    unique_targets = set(targets)
    if len(unique_targets) == 0:
        return torch.tensor(0.)

    if sampling == "per_target":
        for target in unique_targets:
            positive_index = np.random.choice(np.where(targets == target)[0], 1)
            negative_index = np.random.choice(np.where(targets != target)[0], 1)

            positive_indexes.append(positive_index)
            negative_indexes.append(negative_index)
    elif sampling == "old_vs_new_random":
        for old_index, target in enumerate(targets):
            if target >= new_classes_index:
                continue  # Sample belongs to new classes.

            new_index = np.random.choice(np.where(targets >= new_classes_index)[0])
            positive_indexes.append(old_index)
            negative_indexes.append(new_index)
    elif sampling == "per_sample":
        for positive_index, target in enumerate(targets):
            negative_index = np.random.choice(np.where(targets != target)[0], 1)

            positive_indexes.append(positive_index)
            negative_indexes.append(negative_index)
    elif sampling == "full":
        pair_indexes = set()
        for positive_index, target in enumerate(targets):
            for negative_index in np.where(targets != target)[0]:
                pair_indexes.add(tuple(sorted((positive_index, negative_index))))

        for p, n in pair_indexes:
            positive_indexes.append(p)
            negative_indexes.append(n)
    else:
        raise ValueError("Unknown sampling type {}.".format(sampling))

    assert len(positive_indexes) == len(negative_indexes)

    if len(positive_indexes) == 0:
        return 0.

    positive_indexes = torch.LongTensor(positive_indexes)
    negative_indexes = torch.LongTensor(negative_indexes)

    positive_features = features[positive_indexes]
    negative_features = features[negative_indexes]

    similarities = torch.sum(torch.mul(positive_features, negative_features), 1)
    features_dim = features.shape[1]

    first_moment = torch.mean(similarities)
    second_moment = torch.mean(torch.pow(similarities, 2))

    loss = torch.pow(first_moment, 2) + torch.clamp(second_moment - 1. / features_dim, min=0.)

    return factor * loss


def double_soft_orthoreg(weights, config):
    """Extention of the Soft Ortogonality reg, forces the Gram matrix of the
    weight matrix to be close to identity.

    Also called DSO.

    References:
        * Can We Gain More from Orthogonality Regularizations in Training Deep CNNs?
          Bansal et al.
          NeurIPS 2018

    :param weights: Learned parameters of shape (n_classes, n_features).
    :return: A float scalar loss.
    """
    wTw = torch.mm(weights.t(), weights)
    so_1 = torch.frobenius_norm(wTw - torch.eye(wTw.shape[0]).to(weights.device))

    wwT = torch.mm(weights, weights.t())
    so_2 = torch.frobenius_norm(wwT - torch.eye(wwT.shape[0]).to(weights.device))

    if config["squared"]:
        so_1 = torch.pow(so_1, 2)
        so_2 = torch.pow(so_2, 2)

    return config["factor"] * (so_1 + so_2)


def mutual_coherence_regularization(weights, config):
    """Forces weights orthogonality by reducing the highest correlation between
    the weights.

    Also called MC.

    References:
        * Compressed sensing
          David L Donoho.
          Transactions on information theory 2016

    :param weights: Learned parameters of shape (n_classes, n_features).
    :return: A float scalar loss.
    """
    wTw = torch.mm(weights.t(), weights)
    x = wTw - torch.eye(wTw.shape[0]).to(weights.device)

    loss = utils.matrix_infinity_norm(x)

    return config["factor"] * loss


def spectral_restricted_isometry_property_regularization(weights, config):
    """Requires that every set of columns of the weights, with cardinality no
    larger than k, shall behave like an orthogonal system.

    Also called SRIP.

    References:
        * Can We Gain More from Orthogonality Regularizations in Training Deep CNNs?
          Bansal et al.
          NeurIPS 2018

    :param weights: Learned parameters of shape (n_classes, n_features).
    :return: A float scalar loss.
    """
    wTw = torch.mm(weights.t(), weights)
    x = wTw - torch.eye(wTw.shape[0]).to(weights.device)

    _, s, _ = torch.svd(x)

    loss = s[0]
    return config["factor"] * loss


def softriple_regularizer(weights, config):
    weights = F.normalize(weights)

    K = config["K"]
    C = weights.shape[0] // K

    centers_per_class = weights.view(C, K, -1)

    triu_indexes = np.triu_indices(K, 1)
    indexes_0, indexes_1 = torch.tensor(triu_indexes[0]), torch.tensor(triu_indexes[1])

    similarities = torch.bmm(centers_per_class, centers_per_class.transpose(2, 1))
    x = torch.abs(2 - 2 * similarities[..., indexes_0, indexes_1])
    x = torch.sqrt(x + 1e-10)
    loss = x.sum() / (C * K * (K - 1))

    return config["factor"] * loss


def double_margin_constrastive_regularization(
    weights,
    current_index,
    K=None,
    intra_margin=0.2,
    inter_margin=0.8,
    inter_old_vs_new=False,
    normalize=True,
    intra_aggreg="mean",
    inter_aggreg="mean",
    square=True,
    old_weights=None,
    adaptative_margin=False,
    adaptative_margin_max=2.0,
    adaptative_margin_min=0.5,
    factor=1.
):
    """To be used with multiple centers per class. Enforce that weights of different
    classes are further than a given margin intra_margin and weights of same class
    are close but still further than a margin inter_margin.

    intra_margin must be > than inter_margin.

    Note that distance range is:
        * [0, 2]    if squared
        * [0, 1.41] otherwise
    Therefore while the intra_margin should be kept low, the inter_dist if set
    higher than the upper bound will force perfect orthogonality.

    :param weights: Learned parameters of shape (n_classes * n_clusters, n_features).
    :param current_index: The current weight index, i.e. if we have learned N classes, the index
                          will be N.
    :param K: Number of clusters per class.
    :param intra_margin: Margin between clusters of same class.
    :param inter_margin: Margin between clusters of different classes.
    :param inter_old_vs_new: Apply the inter distance only between old & new.
    :param factor: A multiplicative factor applied to the loss.
    :return: A float scalar loss.
    """
    if intra_margin is None and inter_margin is None:
        raise ValueError("At least one margin must be enabled.")

    if normalize:
        weights = F.normalize(weights)

    C = weights.shape[0] // K
    dist = _dmr_weights_distance(weights, square=square)

    loss = 0.

    if intra_margin is not None and K > 1:
        intra_mask = _dmr_intra_mask(dist.shape[0], C, K)
        intra_dist = dist[intra_mask]
        intra_losses = torch.clamp(intra_margin - intra_dist, min=0.)

        intra_loss = _dmr_aggreg(intra_losses, aggreg_mode=intra_aggreg)
        loss += intra_loss

    if inter_margin is not None and not (inter_old_vs_new and current_index == 0):
        if inter_old_vs_new:
            inter_mask = _dmr_inter_oldvsnew_mask(dist.shape[0], current_index)
            inter_dist = dist[inter_mask]
        elif adaptative_margin and old_weights is not None:
            old_dist = _dmr_weights_distance(old_weights, square=square).to(weights.device)
            nb_old_classes = old_weights.shape[0] // K

            inter_mask_old = _dmr_inter_mask(old_dist.shape[0], nb_old_classes, K)
            inter_mask_oldnew = _dmr_inter_mask(dist.shape[0], C, K)
            inter_mask_oldnew[nb_old_classes * K:] = False
            inter_mask_oldnew[..., nb_old_classes * K:] = False

            inter_mask_new = _dmr_inter_mask(dist.shape[0], C, K)
            inter_mask_new[:nb_old_classes * K, :nb_old_classes * K] = False

            old_inter_dist = old_dist[inter_mask_old]
            d = torch.clamp(old_inter_dist, min=0.)
            adaptative_margins = (
                (adaptative_margin_max - adaptative_margin_min) / torch.max(d)
            ) * d + adaptative_margin_min

            oldnew_inter_dist = dist[inter_mask_oldnew]

            new_inter_dist = dist[inter_mask_new]

            inter_dist = torch.cat((oldnew_inter_dist, new_inter_dist))
            inter_margin = torch.cat(
                (
                    adaptative_margins, torch.tensor(inter_margin).repeat(len(new_inter_dist)
                                                                         ).to(weights.device)
                )
            )
            assert len(oldnew_inter_dist) == len(old_inter_dist) == len(adaptative_margins)
        else:
            inter_mask = _dmr_inter_mask(dist.shape[0], C, K)
            inter_dist = dist[inter_mask]

        inter_losses = torch.clamp(inter_margin - inter_dist, min=0.)
        inter_loss = _dmr_aggreg(inter_losses, aggreg_mode=inter_aggreg)
        loss += inter_loss

    if isinstance(loss, float):
        loss = torch.tensor(0.).to(weights.device)

    return factor * loss


def _dmr_inter_mask(size, nb_classes, nb_clusters):
    inter_mask = ~torch.ones(size, size).bool()
    lower_tri = torch.tensor(np.tril_indices(size, k=0))

    for c in range(nb_classes):
        inter_mask[c * nb_clusters:(c + 1) * nb_clusters, (c + 1) * nb_clusters:] = True
    inter_mask[lower_tri[0], lower_tri[1]] = False

    return inter_mask


def _dmr_inter_oldvsnew_mask(size, current_index):
    inter_mask = ~torch.ones(size, size).bool()
    lower_tri = torch.tensor(np.tril_indices(size, k=0))

    inter_mask[:current_index, current_index:] = True
    inter_mask[lower_tri[0], lower_tri[1]] = False

    return inter_mask


def _dmr_intra_mask(size, nb_classes, nb_clusters):
    intra_mask = ~torch.ones(size, size).bool()
    lower_tri = torch.tensor(np.tril_indices(size, k=0))

    for c in range(nb_classes):
        intra_mask[c * nb_clusters:(c + 1) * nb_clusters, :(c + 1) * nb_clusters] = True
    intra_mask[lower_tri[0], lower_tri[1]] = False

    return intra_mask


def _dmr_weights_distance(weights, square=True):
    dist = 2 - 2 * torch.mm(weights, weights.t())
    dist = torch.abs(dist)  # Absolute is to handle small negatives due to numerical instability

    if not square:
        dist = torch.sqrt(torch.abs(dist) + 1e-10)

    return dist


def _dmr_aggreg(losses, aggreg_mode="mean"):
    if aggreg_mode == "mean":
        return torch.mean(losses)
    elif aggreg_mode == "max":
        return torch.max(losses)
    elif aggreg_mode == "adamine":
        nb_not_neg = max(len(losses[losses > 0.]), 1.0)
        return losses.sum() / nb_not_neg

    raise NotImplementedError("Unknown aggreg mode {}.".format(aggreg_mode))