import argparse
import copy
import functools
import json
import random
import sys
import time

import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torchvision
from torchvision import datasets, transforms

import utils
from plot import plot_svrg_run


class SVRGTrainer:
    def __init__(self, create_model, loss_fn):
        self.create_model = create_model
        self.loss_fn = loss_fn

    def train(self, train_loader, num_warmup_epochs, num_outer_epochs, num_inner_epochs, inner_epoch_fraction,
              warmup_learning_rate, learning_rate, device, weight_decay, choose_random_iterate):
        metrics = []

        model = self.create_model().to(device)
        target_model = self.create_model().to(device)
        print(model)

        # Perform several epochs of SGD as initialization for SVRG
        warmup_optimizer = torch.optim.SGD(
            target_model.parameters(), lr=warmup_learning_rate, weight_decay=weight_decay)
        for warmup_epoch in range(1, num_warmup_epochs + 1):
            warmup_loss = 0
            epoch_start = time.time()
            for batch in train_loader:
                data, label = (x.to(device) for x in batch)
                warmup_optimizer.zero_grad()
                prediction = target_model(data.to(device))
                loss = self.loss_fn(prediction, label.to(device))
                loss.backward()
                warmup_optimizer.step()
                warmup_loss += loss.item() * len(data)
            avg_warmup_loss = warmup_loss / len(train_loader.dataset)
            model_grad_norm = utils.calculate_full_gradient_norm(
                model=target_model, data_loader=train_loader, loss_fn=self.loss_fn, device=device)
            elapsed_time = time.time() - epoch_start
            ex_per_sec = len(train_loader.dataset) / elapsed_time
            metrics.append({'warmup_epoch': warmup_epoch,
                            'train_loss': avg_warmup_loss,
                            'grad_norm': model_grad_norm})
            print('[Warmup {}/{}] loss: {:.04f}, grad_norm: {:.02f} (1k) ex/s: {:.02f}'.format(
                warmup_epoch, num_warmup_epochs, avg_warmup_loss, model_grad_norm, ex_per_sec / 1000))

        for epoch in range(1, num_outer_epochs + 1):
            # Find full target gradient
            mu = utils.calculate_full_gradient(
                model=target_model,
                data_loader=train_loader,
                loss_fn=self.loss_fn,
                device=device
            )

            # Initialize model to target model
            model.load_state_dict(copy.deepcopy(target_model.state_dict()))

            optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
            model_state_dicts = []
            inner_batches = len(train_loader)
            if inner_epoch_fraction is not None:
                inner_batches = int(len(train_loader) * inner_epoch_fraction)
            for sub_epoch in range(1, num_inner_epochs + 1):
                train_loss = 0
                examples_seen = 0
                epoch_start = time.time()
                for batch_idx, batch in enumerate(train_loader):

                    data, label = (x.to(device) for x in batch)
                    optimizer.zero_grad()

                    target_model.zero_grad()
                    target_model_out = target_model(data)
                    target_model_loss = self.loss_fn(target_model_out, label)
                    target_model_loss.backward()
                    target_model_grad = torch.cat(
                        [x.grad.view(-1) for x in target_model.parameters()]).detach()

                    model_weights = torch.cat(
                        [x.view(-1) for x in model.parameters()])
                    model_out = model(data)
                    model_loss = self.loss_fn(model_out, label)

                    # Use SGD on auxiliary loss function
                    # See the SVRG paper section 2 for details
                    aux_loss = model_loss - \
                        torch.dot((target_model_grad - mu).detach(),
                                  model_weights)
                    aux_loss.backward()
                    optimizer.step()

                    train_loss += model_loss.item() * len(data)
                    examples_seen += len(data)
                    copy_state_dict = copy.deepcopy(model.state_dict())
                    # Copy model parameters to CPU first to prevent GPU overflow
                    for k, v in copy_state_dict.items():
                        copy_state_dict[k] = v.cpu()
                    model_state_dicts.append(copy_state_dict)

                    batch_num = batch_idx + 1
                    if batch_num >= inner_batches:
                        break
                avg_train_loss = train_loss / examples_seen
                model_grad_norm = utils.calculate_full_gradient_norm(
                    model=model, data_loader=train_loader, loss_fn=self.loss_fn, device=device)
                elapsed_time = time.time() - epoch_start
                ex_per_sec = len(train_loader.dataset) / elapsed_time
                metrics.append({'outer_epoch': epoch,
                                'inner_epoch': sub_epoch,
                                'train_loss': avg_train_loss,
                                'grad_norm': model_grad_norm})
                print('[Outer {}/{}, Inner {}/{}] loss: {:.04f}, grad_norm: {:.02f}, (1k) ex/s: {:.02f}'.format(epoch,
                    num_outer_epochs, sub_epoch, num_inner_epochs, avg_train_loss, model_grad_norm,
                    ex_per_sec / 1000))  # noqa

            if choose_random_iterate:
                new_target_state_dict = random.choice(model_state_dicts)
            else:
                new_target_state_dict = model_state_dicts[-1]
            target_model.load_state_dict(new_target_state_dict)
        return metrics


def create_mlp(layer_sizes):
    layers = [nn.Flatten()]
    for i in range(1, len(layer_sizes)):
        in_size = layer_sizes[i - 1]
        out_size = layer_sizes[i]
        layers.append(nn.Linear(in_size, out_size))
        layers.append(nn.ReLU())
    layers.pop()
    return nn.Sequential(*layers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int)
    parser.add_argument('--dataset_path', default='~/datasets/pytorch')
    parser.add_argument('--max_dataset_size', type=int)
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--warmup_learning_rate', type=float, default=0.01)
    parser.add_argument('--learning_rate', type=float, default=0.025)
    parser.add_argument('--weight_decay', type=float, default=0.0001)
    parser.add_argument('--layer_sizes', type=int,
                        nargs='+', default=[784, 100, 10])
    parser.add_argument('--device', default='cpu', choices=['cpu', 'cuda'])
    parser.add_argument('--num_warmup_epochs', type=int, default=10)
    parser.add_argument('--num_outer_epochs', type=int, default=100)
    parser.add_argument('--num_inner_epochs', type=int, default=5)
    parser.add_argument('--inner_epoch_fraction', type=float)
    parser.add_argument('--choose_random_iterate', default=False, action='store_true')
    parser.add_argument('--run_name', default='svrg')
    parser.add_argument('--output_path')
    parser.add_argument('--plot', default=False, action='store_true')
    args = parser.parse_args()
    print(json.dumps(args.__dict__, indent=2))

    if args.seed is not None:
        print('Using seed:', args.seed)
        torch.manual_seed(args.seed)
        random.seed(args.seed)

    train_ds = datasets.MNIST(
        args.dataset_path, transform=transforms.ToTensor())
    if args.max_dataset_size is not None and len(train_ds) > args.max_dataset_size:
        print('Limiting dataset size to:', args.max_dataset_size)
        train_ds = torch.utils.data.dataset.Subset(
            train_ds, indices=list(range(args.max_dataset_size)))
    train_loader = torch.utils.data.DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True)

    loss_fn = nn.CrossEntropyLoss()
    create_model = functools.partial(create_mlp, layer_sizes=args.layer_sizes)
    trainer = SVRGTrainer(create_model=create_model, loss_fn=loss_fn)
    metrics = trainer.train(
        train_loader=train_loader,
        num_warmup_epochs=args.num_warmup_epochs,
        num_outer_epochs=args.num_outer_epochs,
        num_inner_epochs=args.num_inner_epochs,
        inner_epoch_fraction=args.inner_epoch_fraction,
        warmup_learning_rate=args.warmup_learning_rate,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        device=torch.device(args.device),
        choose_random_iterate=args.choose_random_iterate)

    output = {
        'script': __file__,
        'argv': sys.argv,
        'args': args.__dict__,
        'metrics': metrics
    }
    if args.output_path is not None:
        with open(args.output_path, 'w') as f:
            json.dump(output, f)
        print('Wrote output to:', args.output_path)

    if args.plot:
        plot_svrg_run(run=output, key='train_loss')
        plt.show()


if __name__ == '__main__':
    main()
