import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from tqdm import trange

from inclearn import factory, utils
from inclearn.models.base import IncrementalLearner


class ICarl(IncrementalLearner):
    """Implementation of iCarl.

    :param args: An argparse parsed arguments object.
    """
    def __init__(self, args):
        super().__init__()

        self._device = args["device"]
        self._memory_size = args["memory_size"]
        self._opt_name = args["optimizer"]
        self._lr = args["lr"]
        self._weight_decay = args["weight_decay"]
        self._n_epochs = args["epochs"]

        self._scheduling = args["scheduling"]
        self._lr_decay = args["lr_decay"]

        self._k = args["memory_size"]
        self._n_classes = args["increment"]

        self._features_extractor = factory.get_resnet(args["convnet"], nf=32)
        self._classifier = nn.Linear(self._features_extractor.out_dim, self._n_classes, bias=True)

        self._examplars = {}
        self._means = None

        self._clf_loss = F.binary_cross_entropy_with_logits
        self._distil_loss = F.binary_cross_entropy_with_logits

        self.to(self._device)

    def forward(self, x):
        x = self._features_extractor(x)
        x = self._classifier(x)
        return x

    # ----------
    # Public API
    # ----------

    def _before_task(self, train_loader, val_loader):
        if self._task == 0:
            self._previous_preds = None
        else:
            print("Computing previous predictions...")
            self._previous_preds = self._compute_predictions(train_loader)
            if val_loader:
                self._previous_preds_val = self._compute_predictions(val_loader)

            self._add_n_classes(self._task_size)

        self._optimizer = factory.get_optimizer(
            self.parameters(),
            self._opt_name,
            self._lr,
            self._weight_decay
        )

        self._scheduler = torch.optim.lr_scheduler.MultiStepLR(
            self._optimizer,
            self._scheduling,
            gamma=self._lr_decay
        )

    def _train_task(self, train_loader, val_loader):
        print("nb ", len(train_loader.dataset))

        prog_bar = trange(self._n_epochs, desc="Losses.")

        val_loss = 0.
        for epoch in prog_bar:
            _clf_loss, _distil_loss = 0., 0.
            c = 0

            self._scheduler.step()

            for idx, (idxes, inputs, targets) in enumerate(train_loader, start=1):
                self._optimizer.zero_grad()

                c += len(idxes)
                inputs, targets = inputs.to(self._device), targets.to(self._device)
                targets = utils.to_onehot(targets, self._n_classes).to(self._device)
                logits = self.forward(inputs)

                clf_loss, distil_loss = self._compute_loss(
                    logits,
                    targets,
                    idxes[1],
                )

                if not utils._check_loss(clf_loss) or not utils._check_loss(distil_loss):
                    import pdb; pdb.set_trace()

                loss = clf_loss + distil_loss

                loss.backward()
                self._optimizer.step()

                _clf_loss += clf_loss.item()
                _distil_loss += distil_loss.item()

                if idx % 10 == 0 or idx >= len(train_loader):
                    prog_bar.set_description(
                    "Clf loss: {}; Distill loss: {}; Val loss: {}".format(
                        round(clf_loss.item(), 3),
                        round(distil_loss.item(), 3),
                        round(val_loss, 3)
                    ))

            if val_loader is not None:
                val_loss = self._compute_val_loss(val_loader)
            prog_bar.set_description(
            "Clf loss: {}; Distill loss: {}; Val loss: {}".format(
                round(_clf_loss / c, 3),
                round(_distil_loss / c, 3),
                round(val_loss, 2)
            ))



    def _after_task(self, data_loader):
        self._reduce_examplars()
        self._build_examplars(data_loader)

    def _eval_task(self, data_loader):
        ypred, ytrue = self._classify(data_loader)
        assert ypred.shape == ytrue.shape

        return ypred, ytrue

    def get_memory_indexes(self):
        return self.examplars

    # -----------
    # Private API
    # -----------

    def _compute_val_loss(self, val_loader):
        total_loss = 0.
        c = 0

        for idx, (idxes, inputs, targets) in enumerate(val_loader, start=1):
            self._optimizer.zero_grad()

            c += len(idxes)

            inputs, targets = inputs.to(self._device), targets.to(self._device)
            targets = utils.to_onehot(targets, self._n_classes).to(self._device)
            logits = self.forward(inputs)

            clf_loss, distil_loss = self._compute_loss(
                logits,
                targets,
                idxes[1],
                train=False
            )

            if not utils._check_loss(clf_loss) or not utils._check_loss(distil_loss):
                import pdb; pdb.set_trace()

            total_loss += (clf_loss + distil_loss).item()

        return total_loss

    def _compute_loss(self, logits, targets, idxes, train=True):
        if self._task == 0:
            # First task, only doing classification loss
            clf_loss = self._clf_loss(logits, targets)
            distil_loss = torch.zeros(1, device=self._device)
        else:
            clf_loss = self._clf_loss(
                logits[..., self._new_task_index:],
                targets[..., self._new_task_index:]
            )

            previous_preds = self._previous_preds if train else self._previous_preds_val
            distil_loss = self._distil_loss(
                logits[..., :self._new_task_index],
                previous_preds[idxes, :self._new_task_index]
            )

        return clf_loss, distil_loss


    def _compute_predictions(self, data_loader):
        preds = torch.zeros(self._n_train_data, self._n_classes, device=self._device)

        for idxes, inputs, _ in data_loader:
            inputs = inputs.to(self._device)
            idxes = idxes[1].to(self._device)

            preds[idxes] = self.forward(inputs).detach()

        return torch.sigmoid(preds)


    def _classify(self, data_loader):
        if self._means is None:
            raise ValueError("Cannot classify without built examplar means,"
                             " Have you forgotten to call `before_task`?")
        if self._means.shape[0] != self._n_classes:
            raise ValueError(
                "The number of examplar means ({}) is inconsistent".format(self._means.shape[0])+\
                " with the number of classes ({}).".format(self._n_classes))

        ypred = []
        ytrue = []

        for _, inputs, targets in data_loader:
            inputs = inputs.to(self._device)

            features = self._features_extractor(inputs).detach()
            preds = self._get_closest(self._means, features)

            ypred.extend(preds)
            ytrue.extend(targets)

        return np.array(ypred), np.array(ytrue)

    @property
    def _m(self):
        """Returns the number of examplars per class."""
        return self._k // self._n_classes

    def _add_n_classes(self, n):
        print("add n classes")
        self._n_classes += n

        weight = self._classifier.weight.data
        bias = self._classifier.bias.data

        self._classifier = nn.Linear(
            self._features_extractor.out_dim, self._n_classes,
            bias=True
        ).to(self._device)

        self._classifier.weight.data[: self._n_classes - n] = weight
        self._classifier.bias.data[: self._n_classes - n] = bias

        print("Now {} examplars per class.".format(self._m))

    def _extract_features(self, loader):
        features = []
        idxes = []

        for (real_idxes, _), inputs, _ in loader:
            inputs = inputs.to(self._device)
            features.append(self._features_extractor(inputs).detach())
            idxes.extend(real_idxes.numpy().tolist())

        features = torch.cat(features)
        mean = torch.mean(features, dim=0, keepdim=True)

        return F.normalize(features), F.normalize(mean)[0], idxes

    @staticmethod
    def _remove_row(matrix, idxes, row_idx):
        new_matrix = torch.cat((matrix[:row_idx, ...], matrix[row_idx + 1 :, ...]))
        del matrix
        return new_matrix, idxes[:row_idx] + idxes[row_idx + 1:]

    @staticmethod
    def _get_closest(centers, features):
        pred_labels = []

        for feature in features:
            distances = torch.pow(centers - feature, 2).sum(-1)
            pred_labels.append(distances.argmin().item())

        return np.array(pred_labels)

    @staticmethod
    def _get_closest_features(center, features):
        normalized_features = F.normalize(features)
        distances = torch.pow(center - normalized_features, 2).sum(-1)
        return distances.argmin().item()

    def _build_examplars(self, loader):
        means = []

        print("Updating examplars for classes {} -> {}.".format(
            0, self._task * self._task_size))
        for class_idx in range(0, self._task * self._task_size):
            loader.dataset.set_idxes(self._examplars[class_idx])
            _, examplar_mean, _ = self._extract_features(loader)
            means.append(F.normalize(examplar_mean, dim=0))

        print("Building examplars for classes {} -> {}.".format(
            self._task * self._task_size, self._n_classes))
        for class_idx in range(self._task * self._task_size, self._n_classes):
            examplars_idxes = []

            loader.dataset.set_classes_range(class_idx, class_idx)

            features, class_mean, idxes = self._extract_features(loader)
            examplars_mean = torch.zeros(self._features_extractor.out_dim, device=self._device)

            for i in range(min(self._m, features.shape[0])):
                idx = self._get_closest_features(
                    class_mean, (features + examplars_mean) / (i + 1)
                )
                examplars_idxes.append(idxes[idx])
                examplars_mean += features[idx]
                features, idxes = self._remove_row(features, idxes, idx)

            means.append(F.normalize(examplars_mean / len(examplars_idxes), dim=0))
            self._examplars[class_idx] = examplars_idxes

        self._means = torch.stack(means)

    @property
    def examplars(self):
        return np.array(
            [
                examplar_idx
                for class_examplars in self._examplars.values()
                for examplar_idx in class_examplars
            ]
        )

    def _reduce_examplars(self):
        print("Reducing examplars.")
        for class_idx in range(len(self._examplars)):
            self._examplars[class_idx] = self._examplars[class_idx][: self._m]
