import argparse
import glob
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pytorch_lightning as pl
import torch
from torch.utils.data import DataLoader

from lightning_base import BaseTransformer, add_generic_args, generic_train
from transformers import MBartTokenizer, T5ForConditionalGeneration
from transformers.modeling_bart import shift_tokens_right

from matorage import DataConfig
from matorage.torch import Dataset


try:
    from .callbacks import (
        Seq2SeqLoggingCallback,
        get_checkpoint_callback,
        get_early_stopping_callback,
    )
    from .utils import (
        ROUGE_KEYS,
        LegacySeq2SeqDataset,
        Seq2SeqDataset,
        assert_all_frozen,
        calculate_bleu,
        calculate_rouge,
        flatten_list,
        freeze_params,
        get_git_info,
        label_smoothed_nll_loss,
        lmap,
        pickle_save,
        save_git_info,
        save_json,
        use_task_specific_params,
    )
except ImportError:
    from callbacks import (
        Seq2SeqLoggingCallback,
        get_checkpoint_callback,
        get_early_stopping_callback,
    )
    from utils import (
        ROUGE_KEYS,
        LegacySeq2SeqDataset,
        Seq2SeqDataset,
        assert_all_frozen,
        calculate_bleu,
        calculate_rouge,
        flatten_list,
        freeze_params,
        get_git_info,
        label_smoothed_nll_loss,
        lmap,
        pickle_save,
        save_git_info,
        save_json,
        use_task_specific_params,
    )

logger = logging.getLogger(__name__)


class SummarizationModule(BaseTransformer):
    mode = "summarization"
    loss_names = ["loss"]
    metric_names = ROUGE_KEYS
    default_val_metric = "rouge2"

    def __init__(self, hparams, **kwargs):
        super().__init__(hparams, num_labels=None, mode=self.mode, **kwargs)
        use_task_specific_params(self.model, "summarization")
        save_git_info(self.hparams.output_dir)
        self.metrics_save_path = Path(self.output_dir) / "metrics.json"
        self.hparams_save_path = Path(self.output_dir) / "hparams.pkl"
        pickle_save(self.hparams, self.hparams_save_path)
        self.step_count = 0
        self.metrics = defaultdict(list)

        self.target_lens = {
            "train": self.hparams.max_target_length,
            "val": self.hparams.val_max_target_length,
            "test": self.hparams.test_max_target_length,
        }
        assert (
            self.target_lens["train"] <= self.target_lens["val"]
        ), f"target_lens: {self.target_lens}"
        assert (
            self.target_lens["train"] <= self.target_lens["test"]
        ), f"target_lens: {self.target_lens}"

        if self.hparams.freeze_embeds:
            self.freeze_embeds()
        if self.hparams.freeze_encoder:
            freeze_params(self.model.get_encoder())
            assert_all_frozen(self.model.get_encoder())

        self.hparams.git_sha = get_git_info()["repo_sha"]
        self.num_workers = hparams.num_workers
        self.decoder_start_token_id = None  # default to config
        if self.model.config.decoder_start_token_id is None and isinstance(
            self.tokenizer, MBartTokenizer
        ):
            self.decoder_start_token_id = self.tokenizer.lang_code_to_id[
                hparams.tgt_lang
            ]
            self.model.config.decoder_start_token_id = self.decoder_start_token_id

        self.eval_beams = (
            self.model.config.num_beams
            if self.hparams.eval_beams is None
            else self.hparams.eval_beams
        )
        assert (
            self.eval_beams >= 1
        ), f"got self.eval_beams={self.eval_beams}. Need an integer > 1"
        self.val_metric = (
            self.default_val_metric
            if self.hparams.val_metric is None
            else self.hparams.val_metric
        )

    def freeze_embeds(self):
        """Freeze token embeddings and positional embeddings for bart, just token embeddings for t5."""
        try:
            freeze_params(self.model.model.shared)
            for d in [self.model.model.encoder, self.model.model.decoder]:
                freeze_params(d.embed_positions)
                freeze_params(d.embed_tokens)
        except AttributeError:
            freeze_params(self.model.shared)
            for d in [self.model.encoder, self.model.decoder]:
                freeze_params(d.embed_tokens)

    def forward(self, input_ids, patch_ids, **kwargs):
        return self.model(input_ids, patch_ids, **kwargs)

    def ids_to_clean_text(self, generated_ids: List[int]):
        gen_text = self.tokenizer.batch_decode(
            generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        return lmap(str.strip, gen_text)

    def _step(self, batch: dict) -> Tuple:
        pad_token_id = self.tokenizer.pad_token_id
        src_ids, src_mask, src_patch = batch[0].long(), batch[1].long(), batch[2].long()
        tgt_ids = batch[3].long()
        if isinstance(self.model, T5ForConditionalGeneration):
            decoder_input_ids = self.model._shift_right(tgt_ids)
        else:
            decoder_input_ids = shift_tokens_right(tgt_ids, pad_token_id)

        outputs = self(
            src_ids,
            src_patch,
            attention_mask=src_mask,
            decoder_input_ids=decoder_input_ids,
            use_cache=False,
        )
        lm_logits = outputs[0]
        if self.hparams.label_smoothing == 0:
            # Same behavior as modeling_bart.py, besides ignoring pad_token_id
            loss_fct = torch.nn.CrossEntropyLoss(ignore_index=pad_token_id)

            assert lm_logits.shape[-1] == self.model.config.vocab_size
            loss = loss_fct(lm_logits.view(-1, lm_logits.shape[-1]), tgt_ids.view(-1))
        else:
            lprobs = torch.nn.functional.log_softmax(lm_logits, dim=-1)
            loss, nll_loss = label_smoothed_nll_loss(
                lprobs, tgt_ids, self.hparams.label_smoothing, ignore_index=pad_token_id
            )
        return (loss,)

    @property
    def pad(self) -> int:
        return self.tokenizer.pad_token_id

    def training_step(self, batch, batch_idx) -> Dict:
        loss_tensors = self._step(batch)

        logs = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        # tokens per batch
        logs["tpb"] = (
            batch[0].long().ne(self.pad).sum() + batch[3].long().ne(self.pad).sum()
        )
        return {"loss": loss_tensors[0], "log": logs}

    def validation_step(self, batch, batch_idx) -> Dict:
        return self._generative_step(batch)

    def validation_epoch_end(self, outputs, prefix="val") -> Dict:
        self.step_count += 1
        losses = {
            k: torch.stack([x[k] for x in outputs]).mean() for k in self.loss_names
        }
        loss = losses["loss"]
        rouges = {
            k: np.array([x[k] for x in outputs]).mean()
            for k in self.metric_names + ["gen_time", "gen_len"]
        }
        rouge_tensor: torch.FloatTensor = torch.tensor(rouges[self.val_metric]).type_as(
            loss
        )
        rouges.update({k: v.item() for k, v in losses.items()})
        losses.update(rouges)
        metrics = {f"{prefix}_avg_{k}": x for k, x in losses.items()}
        metrics["step_count"] = self.step_count
        self.save_metrics(metrics, prefix)  # writes to self.metrics_save_path
        preds = flatten_list([x["preds"] for x in outputs])
        return {
            "log": metrics,
            "preds": preds,
            f"{prefix}_loss": loss,
            f"{prefix}_{self.val_metric}": rouge_tensor,
        }

    def save_metrics(self, latest_metrics, type_path) -> None:
        self.metrics[type_path].append(latest_metrics)
        save_json(self.metrics, self.metrics_save_path)

    def calc_generative_metrics(self, preds, target) -> Dict:
        return calculate_rouge(preds, target)

    def _generative_step(self, batch: dict) -> dict:
        t0 = time.time()
        generated_ids = self.model.generate(
            batch[0].long(),
            patch_ids=batch[2].long(),
            attention_mask=batch[1].long(),
            use_cache=True,
            decoder_start_token_id=self.decoder_start_token_id,
        )
        gen_time = (time.time() - t0) / batch[0].shape[0]
        preds: List[str] = self.ids_to_clean_text(generated_ids)
        target: List[str] = self.ids_to_clean_text(batch[3])
        loss_tensors = self._step(batch)
        base_metrics = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        rouge: Dict = self.calc_generative_metrics(preds, target)
        summ_len = np.mean(lmap(len, generated_ids))
        base_metrics.update(
            gen_time=gen_time, gen_len=summ_len, preds=preds, target=target, **rouge
        )
        return base_metrics

    def test_step(self, batch, batch_idx):
        return self._generative_step(batch)

    def test_epoch_end(self, outputs):
        return self.validation_epoch_end(outputs, prefix="test")

    def get_dataset(self, type_path) -> Seq2SeqDataset:
        max_target_length = self.target_lens[type_path]
        data_config = DataConfig(
            endpoint=args.endpoint,
            access_key=os.environ["access_key"],
            secret_key=os.environ["secret_key"],
            region=args.region,
            dataset_name="commit-autosuggestions",
            additional={
                "mode": ("training" if type_path == "train" else "evaluation"),
                "max_source_length": self.hparams.max_source_length,
                "max_target_length": max_target_length,
                "url": args.url,
            },
            attributes=[
                ("input_ids", "int32", (self.hparams.max_source_length,)),
                ("attention_masks", "int32", (self.hparams.max_source_length,)),
                ("patch_ids", "int32", (self.hparams.max_source_length,)),
                ("targets", "int32", (max_target_length,)),
            ],
        )
        return Dataset(config=data_config, clear=True)

    def get_dataloader(
        self, type_path: str, batch_size: int, shuffle: bool = False
    ) -> DataLoader:
        dataset = self.get_dataset(type_path)
        sampler = None

        dataloader = DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            num_workers=self.num_workers,
            sampler=sampler,
        )
        return dataloader

    def train_dataloader(self) -> DataLoader:
        dataloader = self.get_dataloader(
            "train", batch_size=self.hparams.train_batch_size, shuffle=True
        )
        return dataloader

    def val_dataloader(self) -> DataLoader:
        return self.get_dataloader("val", batch_size=self.hparams.eval_batch_size)

    def test_dataloader(self) -> DataLoader:
        return self.get_dataloader("test", batch_size=self.hparams.eval_batch_size)

    @staticmethod
    def add_model_specific_args(parser, root_dir):
        BaseTransformer.add_model_specific_args(parser, root_dir)
        add_generic_args(parser, root_dir)
        parser.add_argument("--url", type=str, required=True, help="github url")
        parser.add_argument(
            "--endpoint",
            type=str,
            required=True,
            help="matorage endpoint, check document of matorage: https://matorage.readthedocs.io/en/stable/storage.html",
        )
        parser.add_argument(
            "--region",
            type=str,
            default=None,
            help="matorage s3 region, check document of matorage: https://matorage.readthedocs.io/en/stable/storage.html",
        )
        parser.add_argument(
            "--max_source_length",
            default=1024,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--max_target_length",
            default=56,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--val_max_target_length",
            default=142,  # these defaults are optimized for CNNDM. For xsum, see README.md.
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument(
            "--test_max_target_length",
            default=142,
            type=int,
            help="The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded.",
        )
        parser.add_argument("--freeze_encoder", action="store_true")
        parser.add_argument("--freeze_embeds", action="store_true")
        parser.add_argument("--sortish_sampler", action="store_true", default=False)
        parser.add_argument(
            "--logger_name",
            type=str,
            choices=["default", "wandb", "wandb_shared"],
            default="default",
        )
        parser.add_argument(
            "--n_train",
            type=int,
            default=-1,
            required=False,
            help="# examples. -1 means use all.",
        )
        parser.add_argument(
            "--n_val",
            type=int,
            default=500,
            required=False,
            help="# examples. -1 means use all.",
        )
        parser.add_argument(
            "--n_test",
            type=int,
            default=-1,
            required=False,
            help="# examples. -1 means use all.",
        )
        parser.add_argument(
            "--task",
            type=str,
            default="summarization",
            required=False,
            help="# examples. -1 means use all.",
        )
        parser.add_argument(
            "--label_smoothing", type=float, default=0.0, required=False
        )
        parser.add_argument("--src_lang", type=str, default="", required=False)
        parser.add_argument("--tgt_lang", type=str, default="", required=False)
        parser.add_argument("--eval_beams", type=int, default=None, required=False)
        parser.add_argument("--val_metric", type=str, default=None, required=False)
        parser.add_argument(
            "--early_stopping_patience",
            type=int,
            default=-1,
            required=False,
            help="-1 means never early stop. early_stopping_patience is measured in validation checks, not epochs. So val_check_interval will effect it.",
        )
        return parser


class TranslationModule(SummarizationModule):
    mode = "translation"
    loss_names = ["loss"]
    metric_names = ["bleu"]
    default_val_metric = "bleu"

    def __init__(self, hparams, **kwargs):
        super().__init__(hparams, **kwargs)
        self.dataset_kwargs["src_lang"] = hparams.src_lang
        self.dataset_kwargs["tgt_lang"] = hparams.tgt_lang

    def calc_generative_metrics(self, preds, target) -> dict:
        return calculate_bleu(preds, target)


def main(args, model=None) -> SummarizationModule:
    Path(args.output_dir).mkdir(exist_ok=True)
    if len(os.listdir(args.output_dir)) > 3 and args.do_train:
        raise ValueError(
            "Output directory ({}) already exists and is not empty.".format(
                args.output_dir
            )
        )
    if model is None:
        if args.task == "summarization":
            model: SummarizationModule = SummarizationModule(args)
        else:
            model: SummarizationModule = TranslationModule(args)

    logger = True
    es_callback = False
    trainer: pl.Trainer = generic_train(
        model,
        args,
        logging_callback=Seq2SeqLoggingCallback(),
        checkpoint_callback=get_checkpoint_callback(args.output_dir, model.val_metric),
        early_stopping_callback=es_callback,
        logger=logger,
        # TODO: early stopping callback seems messed up
    )
    pickle_save(model.hparams, model.output_dir / "hparams.pkl")
    if not args.do_predict:
        return model

    model.hparams.test_checkpoint = ""
    checkpoints = list(
        sorted(glob.glob(os.path.join(args.output_dir, "*.ckpt"), recursive=True))
    )
    if checkpoints:
        model.hparams.test_checkpoint = checkpoints[-1]
        trainer.resume_from_checkpoint = checkpoints[-1]
    trainer.logger.log_hyperparams(model.hparams)

    # test() without a model tests using the best checkpoint automatically
    trainer.test()
    return model


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser = pl.Trainer.add_argparse_args(parser)
    parser = SummarizationModule.add_model_specific_args(parser, os.getcwd())

    args = parser.parse_args()

    main(args)
