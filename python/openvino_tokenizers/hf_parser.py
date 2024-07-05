# -*- coding: utf-8 -*-
# Copyright (C) 2018-2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import tempfile
from copy import deepcopy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import openvino.runtime.opset14 as opset
from openvino import Model, PartialShape, Type
from openvino.runtime import Node, op
from openvino.runtime.exceptions import OVTypeError
from openvino.runtime.opset1.ops import _get_node_factory_opset1
from openvino.runtime.utils.types import as_node, make_constant_node
from transformers import PreTrainedTokenizerBase, PreTrainedTokenizerFast
from transformers.convert_slow_tokenizer import import_protobuf

from . import _get_factory
from .constants import (
    ATTENTION_MASK_INPUT_NAME,
    DETOKENIZER_NAME,
    EOS_TOKEN_ID_NAME,
    STRING_OUTPUT_NAME,
    TOKEN_IDS_INPUT_NAME,
    TOKEN_TYPE_IDS_INPUT_NAME,
    TOKENIZER_NAME,
)
from .tokenizer_pipeline import (
    BPETokenizationStep,
    ByteFallbackStep,
    BytesToCharsStep,
    CaseFoldStep,
    CharsToBytesStep,
    CombineSegmentsStep,
    DecodingStep,
    FuseStep,
    NMTNormalizationStep,
    NormalizationStep,
    NormalizeUnicode,
    PaddingStep,
    PreTokenizatinStep,
    RegexDecodingStep,
    RegexNormalizationStep,
    RegexSplitStep,
    StripStringStep,
    TokenizerPipeline,
    TruncationStep,
    VocabDecoderStep,
    WhitespaceSplitStep,
    WordPieceTokenizationStep,
)
from .utils import filter_re2_incompatible


def parse_replace_normalizer(normalizer_dict: Dict[str, Any]) -> List[RegexNormalizationStep]:
    regex_search_pattern = normalizer_dict["pattern"].get("String") or normalizer_dict["pattern"]["Regex"]
    filtered_pattern = filter_re2_incompatible(regex_search_pattern)
    if filtered_pattern == "":
        return []

    return [
        RegexNormalizationStep(
            regex_search_pattern=regex_search_pattern,
            replace_term=normalizer_dict["content"],
        )
    ]


def parse_bert_normalizer(normalizer_dict: Dict[str, Any]) -> List[NormalizationStep]:
    steps: List[NormalizationStep] = []

    if normalizer_dict["clean_text"] is True:
        pass
        # TODO: this regex is not supported by re2, skip it until broader syntax support
        # steps.append(RegexNormalizationStep.del_control_chars_regex())

    # https://github.com/huggingface/tokenizers/blob/8c9cfb0b689bce00b615b9557a9a767f286d7a33/tokenizers/src/normalizers/bert.rs#L127
    if normalizer_dict.get("strip_accents") or normalizer_dict["lowercase"]:
        steps.append(NormalizeUnicode("NFD"))
        steps.append(RegexNormalizationStep.strip_accents_regex())

    if normalizer_dict["lowercase"] is True:
        steps.append(CaseFoldStep())

    return steps


def parse_strip_step(split_dict: Dict[str, Any]) -> StripStringStep:
    return StripStringStep(
        left=split_dict["strip_left"],
        right=split_dict["strip_right"],
    )


def parse_split_step(pretokenizer_dict: Dict[str, Any]) -> RegexSplitStep:
    split_pattern = pretokenizer_dict["pattern"].get("String") or pretokenizer_dict["pattern"]["Regex"]
    return RegexSplitStep(
        split_pattern=split_pattern,
        invert=pretokenizer_dict["invert"],
        behaviour=pretokenizer_dict["behavior"].lower().rstrip("d"),
    )


def parse_byte_level_pretokenization_step(
    pretokenizer_dict: Dict[str, Any],
) -> List[Union[NormalizationStep, PreTokenizatinStep]]:
    steps = []
    if pretokenizer_dict.get("add_prefix_space"):
        # todo: do not add whitespace if it is already is whitespace
        steps.append(RegexNormalizationStep.add_prefix_whitespace_regex())

    # regex is used by default, but it does not appear in config yet
    if pretokenizer_dict.get("use_regex", True):
        # re2 does not support negative lookahead, so there is two steps replicate the behaviour
        # this WA causes segfault for CLIP tokenizer
        # steps.append(RegexSplitStep.add_whitespace_to_the_next_word())
        steps.append(RegexSplitStep.byte_level_splitter())

    steps.append(BytesToCharsStep())
    return steps


class TransformersTokenizerPipelineParser:
    def __init__(self, tokenizer_object: Any, number_of_inputs: int = 1, add_special_tokens: bool = True) -> None:
        if not tokenizer_object.is_fast:
            raise OVTypeError("Tokenizer is not supported.")

        self.original_tokenizer = tokenizer_object
        with TemporaryDirectory() as tmpdir:
            tokenizer_object.save_pretrained(tmpdir)
            # Windows uses cp1252 encoding by default, need to use utf-8 explicitly
            with open(Path(tmpdir) / "tokenizer.json", encoding="utf-8") as tj:
                self.tokenizer_json = json.load(tj)
        self.pipeline = TokenizerPipeline()
        self.number_of_inputs = number_of_inputs
        self.num_of_added_tokens = 0
        self.add_special_tokens = add_special_tokens

    def parse(
        self,
        number_of_inputs: Optional[int] = None,
        add_special_tokens: bool = True,
        skip_special_tokens: bool = False,
        clean_up_tokenization_spaces: Optional[bool] = None,
        use_max_padding: bool = False,
    ) -> TokenizerPipeline:
        self.number_of_inputs = self.number_of_inputs if number_of_inputs is None else number_of_inputs
        self.pipeline.number_of_inputs = self.number_of_inputs
        for add_steps in [
            self.normalization,
            self.pre_tokenization,
            self.tokenization_model,
            partial(self.post_tokenization, add_special_tokens=add_special_tokens, use_max_padding=use_max_padding),
            partial(
                self.decoding,
                skip_special_tokens=skip_special_tokens,
                clean_up_tokenization_spaces=clean_up_tokenization_spaces,
            ),
        ]:
            add_steps()

        self.pipeline.eos_token_id = self.pipeline.get_eos_token_id(self.original_tokenizer)
        return self.pipeline

    normalizers_map: Dict[
        str,
        Callable[[Dict[str, Any]], Union[NormalizationStep, List[NormalizationStep]]],
    ] = {
        "NFC": lambda step_dict: NormalizeUnicode("NFC"),
        "NFD": lambda step_dict: NormalizeUnicode("NFD"),
        "NFKC": lambda step_dict: NormalizeUnicode("NFKC"),
        "NFKD": lambda step_dict: NormalizeUnicode("NFKD"),
        "Nmt": lambda step_dict: NMTNormalizationStep(),
        "Lowercase": lambda step_dict: CaseFoldStep(),
        "StripAccents": lambda step_dict: RegexNormalizationStep.strip_accents_regex(),
        "BertNormalizer": parse_bert_normalizer,
        "Replace": parse_replace_normalizer,
        "Strip": parse_strip_step,
        "Prepend": lambda step_dict: RegexNormalizationStep.prepend_regex(step_dict.get("prepend", "")),
    }

    def parse_normalizer_step(self, step_dict: Dict[str, Any]) -> None:
        try:
            self.pipeline.add_steps(self.normalizers_map[step_dict["type"]](step_dict))
        except KeyError:
            raise OVTypeError(f"Normalizer type '{step_dict['type']}' is not supported")

    def normalization(self) -> None:
        if self.tokenizer_json["normalizer"] is None:
            return

        if self.tokenizer_json["normalizer"].get("type") == "Sequence":
            for normalizer in self.tokenizer_json["normalizer"]["normalizers"]:
                self.parse_normalizer_step(normalizer)
        else:
            self.parse_normalizer_step(self.tokenizer_json["normalizer"])

    pre_tokenization_map: Dict[
        str,
        Callable[[Dict[str, Any]], Union[PreTokenizatinStep, List[PreTokenizatinStep]]],
    ] = {
        "BertPreTokenizer": lambda step_dict: RegexSplitStep.bert_splitter(),
        "Whitespace": lambda step_dict: RegexSplitStep.whitespace_splitter(),
        "WhitespaceSplit": lambda step_dict: WhitespaceSplitStep(),
        "Split": parse_split_step,
        "Punctuation": lambda step_dict: RegexSplitStep.punctuation_splitter(step_dict["behavior"]),
        "ByteLevel": parse_byte_level_pretokenization_step,
        "Digits": lambda step_dict: RegexSplitStep.digits_splitter(
            "isolate" if step_dict["individual_digits"] else "contiguous"
        ),
    }

    def parse_pre_tokenization_step(self, step_dict: Dict[str, Any]) -> None:
        try:
            self.pipeline.add_steps(self.pre_tokenization_map[step_dict["type"]](step_dict))
        except KeyError:
            raise OVTypeError(f"Pre-tokenizer type '{step_dict['type']}' is not supported")

    def pre_tokenization(self) -> None:
        if self.tokenizer_json["pre_tokenizer"] is None:
            return

        if self.tokenizer_json["pre_tokenizer"].get("type") == "Sequence":
            for pretokenizer in self.tokenizer_json["pre_tokenizer"]["pretokenizers"]:
                self.parse_pre_tokenization_step(pretokenizer)
        else:
            self.parse_pre_tokenization_step(self.tokenizer_json["pre_tokenizer"])

    def tokenization_model(self) -> None:
        if self.tokenizer_json["model"]["type"] == "WordPiece":
            self.pipeline.add_steps(WordPieceTokenizationStep.from_hf_json(self.tokenizer_json))
            self.pipeline.vocab = self.pipeline[-1].vocab
        elif self.tokenizer_json["model"]["type"] == "BPE":
            self.pipeline.add_steps(BPETokenizationStep.from_hf_json(self.tokenizer_json))
            self.pipeline.vocab = self.pipeline[-1].vocab
        else:
            raise OVTypeError(f"Tokenizer type '{self.tokenizer_json['model']['type']}' is not supported")

    post_tokenization_map: Dict[
        str,
        Callable[[Dict[str, Any]], Union[PreTokenizatinStep, List[PreTokenizatinStep]]],
    ] = {
        "TemplateProcessing": CombineSegmentsStep.from_hf_json_template_postprocessor,
        "BertProcessing": CombineSegmentsStep.from_hf_json_bert_postprocessor,
        "RobertaProcessing": CombineSegmentsStep.from_hf_json_roberta_processor,
    }

    def post_tokenization(self, add_special_tokens: bool = True, use_max_padding: bool = False) -> None:
        post_processor_json = self.tokenizer_json["post_processor"]
        if (
            post_processor_json is None
            # As a `PostProcessor`, `ByteLevel` is in charge of trimming the offsets if necessary
            or post_processor_json["type"] == "ByteLevel"
        ):
            self.add_truncation()
            self.add_padding(use_max_padding=use_max_padding)
            return

        pt_type = post_processor_json["type"]

        if pt_type != "Sequence" and pt_type not in self.post_tokenization_map:
            raise OVTypeError(f"Post-processor type '{pt_type}' is not supported")

        if pt_type == "Sequence":
            processors = post_processor_json["processors"]
            combine_segments_step = next(
                (
                    self.post_tokenization_map[step["type"]](step, self.number_of_inputs, add_special_tokens)
                    for step in processors
                    if step["type"] in self.post_tokenization_map
                ),
                None,
            )
            if combine_segments_step is None:
                raise OVTypeError(
                    "Expected that Sequence post-tokenizer type contains one of supported post-tokenizers type:"
                    f"{list(self.post_tokenization_map)}"
                )
        else:
            combine_segments_type = self.post_tokenization_map[pt_type]
            combine_segments_step = combine_segments_type(
                post_processor_json, self.number_of_inputs, add_special_tokens
            )

        self.num_of_added_tokens += combine_segments_step.number_of_added_tokens

        self.add_truncation()
        self.pipeline.add_steps(combine_segments_step)

        self.add_padding(use_max_padding=use_max_padding)

    def add_truncation(self) -> None:
        max_length = getattr(self.original_tokenizer, "model_max_length", -1)

        if self.tokenizer_json["truncation"] is not None:
            self.pipeline.add_steps(
                TruncationStep.from_hf_json(
                    self.tokenizer_json, num_of_added_tokens=self.num_of_added_tokens, max_length=max_length
                )
            )
        elif self.original_tokenizer.model_max_length is not None:
            self.pipeline.add_steps(TruncationStep.from_hf_object(self.original_tokenizer, self.num_of_added_tokens))

    def add_padding(self, use_max_padding: bool = False) -> None:
        max_length = getattr(self.original_tokenizer, "model_max_length", -1)
        pad_token = getattr(self.original_tokenizer, "pad_token")
        pad_token_id = getattr(self.original_tokenizer, "pad_token_id")
        pad_right = getattr(self.original_tokenizer, "padding_side") != "left"

        if self.tokenizer_json["padding"] is not None:
            self.pipeline.add_steps(
                PaddingStep.from_hf_json(
                    tokenizer_json=self.tokenizer_json,
                    pad_to_max_length=use_max_padding,
                    max_length=max_length,
                    pad_right=pad_right,
                )
            )
        else:
            self.pipeline.add_steps(
                PaddingStep(
                    token=pad_token,
                    _token_id=pad_token_id,
                    pad_to_max_length=use_max_padding,
                    max_length=max_length,
                    pad_right=pad_right,
                )
            )

    decoding_map: Dict[
        str,
        Callable[[Dict[str, Any]], Union[DecodingStep, List[DecodingStep]]],
    ] = {
        "Replace": lambda decode_dict: RegexDecodingStep.parse_replace_dict(decode_dict),
        "Fuse": lambda decode_dict: FuseStep(),
        "Strip": lambda decode_dict: RegexDecodingStep.parse_strip_dict(decode_dict),
        "ByteFallback": lambda decode_dict: ByteFallbackStep(),
    }

    def decoding(
        self,
        skip_special_tokens: bool = False,
        clean_up_tokenization_spaces: Optional[bool] = None,
    ) -> None:
        if self.tokenizer_json["decoder"] is None or self.tokenizer_json["model"]["type"] == "WordPiece":
            return

        skip_tokens = parse_special_tokens(self.original_tokenizer) if skip_special_tokens else {}
        self.pipeline.add_steps(VocabDecoderStep(skip_tokens=list(skip_tokens)))

        if self.tokenizer_json["decoder"]["type"] == "Sequence":
            for decoder_dict in self.tokenizer_json["decoder"]["decoders"]:
                decoder_parser = self.decoding_map.get(decoder_dict.get("type"))
                if decoder_parser is None:
                    pass
                    # raise ValueError(f"Decoder {decoder_dict} is not supported yet.")
                else:
                    self.pipeline.add_steps(decoder_parser(decoder_dict))
        elif self.tokenizer_json["decoder"]["type"] == "ByteLevel":
            self.pipeline.add_steps(CharsToBytesStep())
        else:
            self.pipeline.add_steps(FuseStep())

        if suffix := self.tokenizer_json["model"].get("end_of_word_suffix"):
            self.pipeline.add_steps(RegexDecodingStep.replace_end_of_word_suffix(suffix=suffix))

        if prefix := self.tokenizer_json["model"].get("continuing_subword_prefix"):
            self.pipeline.add_steps(RegexDecodingStep.replace_continuing_subword_prefix(prefix=prefix))

        if clean_up_tokenization_spaces is None:
            clean_up_tokenization_spaces = self.original_tokenizer.clean_up_tokenization_spaces

        if clean_up_tokenization_spaces and self.pipeline.decoding_steps:
            self.pipeline.add_steps(RegexDecodingStep.clean_up_tokenization_spaces())
        return


def parse_special_tokens(hf_tokenizer: PreTrainedTokenizerBase, only_special_tokens: bool = True) -> Dict[int, str]:
    # the order matters
    if getattr(hf_tokenizer, "added_tokens_decoder", None):
        return {
            idx: added_token.content
            for idx, added_token in hf_tokenizer.added_tokens_decoder.items()
            if not only_special_tokens or added_token.special
        }
    elif hasattr(hf_tokenizer, "tokenizer") and hasattr(hf_tokenizer.tokenizer, "index_special_tokens"):
        return hf_tokenizer.tokenizer.index_special_tokens
    elif hasattr(hf_tokenizer, "special_tokens"):
        return {idx: token for token, idx in sorted(hf_tokenizer.special_tokens.items(), key=lambda x: x[1])}

    return {}


def convert_fast_tokenizer(
    hf_tokenizer: PreTrainedTokenizerBase,
    number_of_inputs: int = 1,
    with_detokenizer: bool = False,
    add_special_tokens: bool = True,
    skip_special_tokens: bool = False,
    clean_up_tokenization_spaces: Optional[bool] = None,
    use_max_padding: bool = False,
) -> Union[Model, Tuple[Model, Model]]:
    pipeline = TransformersTokenizerPipelineParser(hf_tokenizer).parse(
        number_of_inputs=number_of_inputs,
        add_special_tokens=add_special_tokens,
        skip_special_tokens=skip_special_tokens,
        clean_up_tokenization_spaces=clean_up_tokenization_spaces,
        use_max_padding=use_max_padding,
    )
    ov_tokenizer = pipeline.get_tokenizer_ov_subgraph()
    output_names = hf_tokenizer.model_input_names

    ov_tokenizer_output_names = [TOKEN_IDS_INPUT_NAME, ATTENTION_MASK_INPUT_NAME]
    if len(output_names) == 3 and len(ov_tokenizer.outputs) == 3:
        ov_tokenizer_output_names.insert(1, TOKEN_TYPE_IDS_INPUT_NAME)

    filtered_outputs = []
    for i, output_name in enumerate(ov_tokenizer_output_names):
        current_output = next(
            (output for output in ov_tokenizer.outputs if output.any_name == output_name),
            False,
        )
        if current_output:
            filtered_outputs.append(current_output)
            continue

        if output_name in output_names:
            ov_tokenizer.output(i).tensor.add_names({output_name})
            filtered_outputs.append(ov_tokenizer.output(i))

    tokenizer_model = Model(filtered_outputs, ov_tokenizer.get_parameters(), TOKENIZER_NAME)
    for path, info in ov_tokenizer.get_rt_info().items():
        tokenizer_model.set_rt_info(info.value, path)

    if with_detokenizer:
        return tokenizer_model, pipeline.get_detokenizer_ov_subgraph()

    return tokenizer_model


def is_sentencepiece_model(hf_tokenizer: PreTrainedTokenizerBase) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            hf_tokenizer.save_pretrained(tmp)
        except Exception:
            return False
        if not hasattr(hf_tokenizer, "vocab_files_names") or "vocab_file" not in hf_tokenizer.vocab_files_names:
            return False
        vocab_file = Path(tmp) / hf_tokenizer.vocab_files_names["vocab_file"]
        return (
            getattr(hf_tokenizer, "vocab_files_names", {}).get("vocab_file", "").endswith(".model")
            and vocab_file.exists()
        )


def modify_sentencepiece_model(
    sp_model_path: Path,
    add_tokens: Dict[int, str],
    hf_tokenizer: PreTrainedTokenizerBase,
    skip_special_tokens: bool = False,
    add_prefix_space: Optional[bool] = None,
) -> str:
    model_pb = import_protobuf()
    model = model_pb.ModelProto()
    with open(sp_model_path, "rb") as model_file:
        model.ParseFromString(model_file.read())

    if add_prefix_space is not None:
        model.normalizer_spec.add_dummy_prefix = add_prefix_space

    existing = {piece.piece: piece for piece in model.pieces}
    for idx, token in sorted(add_tokens.items()):
        if to_add := (idx >= len(model.pieces) or model.pieces[idx].piece != token):
            if exists := existing.get(token):
                new_piece = model.pieces.pop(next(idx for idx, piece in enumerate(model.pieces) if piece == exists))
            else:
                new_piece = deepcopy(model.pieces[-1])
                new_piece.piece = token
        else:
            new_piece = model.pieces[idx]

        if skip_special_tokens and new_piece.type not in (2, 4):  # type 2 is for unk symbol
            new_piece.type = 3  # make it control symbol so it will not decode during detokenization
        elif not skip_special_tokens and new_piece.type == 3:
            new_piece.type = 4  # change control type to userdef type

        if to_add:
            while len(model.pieces) + 1 <= idx:
                # to place special token in particular idx we have to extend vocab first
                missing_piece = deepcopy(new_piece)
                missing_piece.piece = hf_tokenizer.decode(len(model.pieces)) or f"<empty_{len(model.pieces)}>"
                missing_piece.type = 4
                model.pieces.insert(idx, missing_piece)
            model.pieces.insert(idx, new_piece)

    while (idx := len(model.pieces)) < getattr(hf_tokenizer, "vocab_size", len(model.pieces)):
        new_piece = deepcopy(model.pieces[-1])
        new_piece.piece = (
            hf_tokenizer.decode(len(model.pieces), skip_special_tokens=False) or f"<empty_{len(model.pieces)}>"
        )
        new_piece.type = 3
        model.pieces.insert(idx, new_piece)

    # change unk token representation from ⁇ to token string
    unk_token = next(piece for piece in model.pieces if piece.type == 2)
    model.trainer_spec.unk_surface = unk_token.piece

    return model.SerializeToString()


def convert_sentencepiece_model_tokenizer(
    hf_tokenizer: PreTrainedTokenizerBase,
    add_attention_mask: bool = True,
    with_detokenizer: bool = False,
    streaming_detokenizer: bool = False,
    add_special_tokens: bool = True,
    skip_special_tokens: bool = False,
    clean_up_tokenization_spaces: Optional[bool] = False,
    add_prefix_space: Optional[bool] = None,
) -> Union[Model, Tuple[Model, Model]]:
    if not is_sentencepiece_model(hf_tokenizer):
        raise OVTypeError("Cannot convert tokenizer of this type without `.model` file.")

    is_chatglm = getattr(hf_tokenizer, "name", None) == "GLMTokenizer"
    add_bos_token = add_eos_token = None
    if is_chatglm:
        add_eos_token = False
    elif hasattr(hf_tokenizer, "build_inputs_with_special_tokens"):
        _fake_token_id = -0.5
        try:
            _ids = hf_tokenizer.build_inputs_with_special_tokens([_fake_token_id])
            add_bos_token = _ids[0] != _fake_token_id
            add_eos_token = _ids[-1] != _fake_token_id
        except Exception:
            # some tokenizers have broken build_inputs_with_special_tokens method,
            # fallback older add bos/eos token detection methods
            pass

    if add_eos_token is None and hasattr(hf_tokenizer, "add_eos_token"):
        add_eos_token = hf_tokenizer.add_eos_token or False
    elif add_eos_token is None:
        add_eos_token = (
            getattr(hf_tokenizer, "truncation_side", "") == "right"
            or getattr(hf_tokenizer, "padding_side", "") == "right"
        )

    if add_bos_token is None:
        add_bos_token = (
            getattr(hf_tokenizer, "add_bos_token", add_eos_token) and hf_tokenizer.bos_token_id is not None
        ) or False

    if add_special_tokens is False:
        add_bos_token = add_eos_token = False

    with tempfile.TemporaryDirectory() as tmp:
        hf_tokenizer.save_pretrained(tmp)
        vocab_file = Path(tmp) / hf_tokenizer.vocab_files_names["vocab_file"]
        if not vocab_file.exists():
            raise OVTypeError("Cannot convert tokenizer of this type without `.model` file.")

        tokenizer_json_file = Path(tmp) / "tokenizer.json"
        prepend_scheme = ""
        if (
            add_prefix_space is None
            and isinstance(hf_tokenizer, PreTrainedTokenizerFast)
            and tokenizer_json_file.exists()
        ):
            # specify encoding for windows - uses cp-1252 otherwise
            with open(tokenizer_json_file, encoding="utf-8") as f:
                tokenizer_json = json.load(f)
                pre_tokenizer = tokenizer_json.get("pre_tokenizer")
                if pre_tokenizer and pre_tokenizer.get("type") == "Metaspace":
                    metaspace = pre_tokenizer
                elif pre_tokenizer and pre_tokenizer.get("type") == "Sequence":
                    metaspace = next(
                        (pre for pre in pre_tokenizer["pretokenizers"] if pre["type"] == "Metaspace"), None
                    )
                else:
                    metaspace = None

                if metaspace is not None:
                    prepend_scheme = metaspace.get("prepend_scheme", "")
                    if prepend_scheme == "always":
                        add_prefix_space = True
                    elif prepend_scheme == "never":
                        add_prefix_space = False
                    elif prepend_scheme == "first":
                        add_prefix_space = False

                # metaspace can be emulated with sequence of normalizers
                if add_prefix_space is None:
                    normalizers = tokenizer_json.get("normalizer", {}).get("normalizers", [])
                    add_prefix_space = any(normalizer.get("prepend") == "▁" for normalizer in normalizers)
                    prepend_scheme = "never"

        elif add_prefix_space is None and isinstance(hf_tokenizer, PreTrainedTokenizerFast):
            add_prefix_space = not add_bos_token

        add_tokens = parse_special_tokens(hf_tokenizer, only_special_tokens=False)

        sp_model_string = modify_sentencepiece_model(
            sp_model_path=vocab_file,
            add_tokens=add_tokens,
            hf_tokenizer=hf_tokenizer,
            skip_special_tokens=False,
            add_prefix_space=add_prefix_space,
        )
        sp_model = np.frombuffer(sp_model_string, dtype=np.uint8)
        sp_model_node = as_node(sp_model)

        sp_detokenizer_model_string = modify_sentencepiece_model(
            sp_model_path=vocab_file,
            add_tokens=add_tokens,
            hf_tokenizer=hf_tokenizer,
            skip_special_tokens=skip_special_tokens,
            add_prefix_space=add_prefix_space,
        )
        sp_detokenizer_model = np.fromstring(sp_detokenizer_model_string, dtype=np.uint8)
        sp_detokenizer_model_node = as_node(sp_detokenizer_model)

    input_node = op.Parameter(Type.string, PartialShape(["?"]))
    input_node.set_friendly_name("string_input")
    next_node = input_node.outputs()

    if prepend_scheme == "first":
        next_node = _get_factory().create("StringTensorUnpack", next_node).outputs()
        next_node = RegexNormalizationStep.add_prefix_whitespace_to_not_whitespace_regex().get_ov_subgraph(next_node)
        next_node = _get_factory().create("StringTensorPack", next_node).outputs()

    do_left_padding = hf_tokenizer.padding_side == "left"

    tokenizer_node = _get_factory().create(
        "SentencepieceTokenizer",
        [sp_model_node, *next_node],
        {
            "add_bos": add_bos_token,
            "add_eos": add_eos_token,
            "reverse": do_left_padding,
            "alpha": 0.0,
        },
    )

    indices, values, dense_shape = tokenizer_node.outputs()

    if add_attention_mask or do_left_padding:
        attention_mask = _get_factory().create(
            "ScatterNDUpdate",
            [
                opset.broadcast(make_constant_node(0, values.element_type), dense_shape),
                indices,
                opset.broadcast(
                    make_constant_node(1, values.element_type),
                    opset.shape_of(values),
                ),
            ],
        )

    if is_chatglm and add_special_tokens:
        prefix_tokens = np.array([hf_tokenizer.get_prefix_tokens()])
        dense_shape, indices, values, attention_mask = add_prefix_tokens(
            prefix_tokens, dense_shape, indices, values, attention_mask, do_left_padding
        )

    default_value = make_constant_node(hf_tokenizer.pad_token_id or 0, values.element_type)
    broadcast = opset.broadcast(default_value, dense_shape, broadcast_spec="BIDIRECTIONAL")

    scattered_input_ids = _get_factory().create(
        "ScatterNDUpdate",
        [broadcast, indices, values],
    )

    if do_left_padding:
        attention_mask = _get_node_factory_opset1().create(
            "Reverse", [attention_mask, make_constant_node(np.array([-1]))], {"mode": "index"}
        )
        scattered_input_ids = _get_node_factory_opset1().create(
            "Reverse", [scattered_input_ids, make_constant_node(np.array([-1]))], {"mode": "index"}
        )

    scattered_input_ids.output(0).tensor.add_names({TOKEN_IDS_INPUT_NAME})
    outputs = scattered_input_ids.outputs()

    if add_attention_mask:
        attention_mask.output(0).tensor.add_names({ATTENTION_MASK_INPUT_NAME})
        outputs.append(attention_mask.output(0))

    tokenizer = Model(outputs, [input_node], TOKENIZER_NAME)
    tokenizer.validate_nodes_and_infer_types()

    eos_token_id = TokenizerPipeline.get_eos_token_id(hf_tokenizer)
    if eos_token_id is not None:
        tokenizer.set_rt_info(eos_token_id, EOS_TOKEN_ID_NAME)

    if not with_detokenizer:
        return tokenizer

    if clean_up_tokenization_spaces is None:
        clean_up_tokenization_spaces = hf_tokenizer.clean_up_tokenization_spaces

    detokenizer = get_sp_detokenizer(
        sp_model_node=sp_detokenizer_model_node,
        streaming_detokenizer=streaming_detokenizer,
        clean_up_tokenization_spaces=clean_up_tokenization_spaces,
        prepend_scheme=prepend_scheme,
        add_prefix_space=add_prefix_space,
    )

    if eos_token_id is not None:
        detokenizer.set_rt_info(eos_token_id, EOS_TOKEN_ID_NAME)

    return tokenizer, detokenizer


def add_prefix_tokens(
    prefix_tokens, dense_shape, indices, values, attention_mask=None, do_left_padding=False
) -> Tuple:
    if do_left_padding is True and attention_mask is None:
        raise ValueError("You must pass attention_mask when add prefix with left padding.")

    if do_left_padding:
        prefix_tokens = prefix_tokens[..., ::-1]  # reverse prefix

    _, prefix_len = prefix_tokens.shape
    index_update_node = make_constant_node(np.array([0, prefix_len]))

    # update resulting dense tensor shape
    dense_shape = opset.add(dense_shape, opset.convert(index_update_node, destination_type=dense_shape.element_type))
    prefix_tokens_node = make_constant_node(prefix_tokens, dtype=values.element_type)
    batch_size = opset.gather(dense_shape, as_node(0), as_node(0))
    batch_slice = opset.slice(dense_shape, as_node([0]), as_node([1]), as_node([1]))
    # new values
    prefix_tokens_batch = opset.broadcast(
        data=prefix_tokens_node,
        target_shape=opset.concat([batch_slice, as_node([prefix_len])], axis=0),
        broadcast_spec="BIDIRECTIONAL",
    )
    prefix_tokens_batch = opset.reshape(prefix_tokens_batch, output_shape=[-1], special_zero=False)
    values = opset.concat([values, prefix_tokens_batch], axis=0)
    # new indices
    prefix_range = opset.range(as_node(0), as_node(prefix_len), as_node(1), output_type=indices.element_type)

    x_indices = opset.range(as_node(0), as_node(batch_size), as_node(1), output_type=indices.element_type)
    x_indices = opset.broadcast(
        data=x_indices,
        target_shape=opset.concat([as_node([prefix_len]), batch_slice], axis=0),
        broadcast_spec="BIDIRECTIONAL",
    )
    x_indices = opset.transpose(x_indices, as_node([1, 0]))
    x_indices = opset.reshape(x_indices, output_shape=[-1, 1], special_zero=False)

    if do_left_padding:
        prefix_start = opset.convert(
            opset.reduce_sum(node=attention_mask, reduction_axes=-1, keep_dims=True), Type.i64
        )
        y_indices = opset.add(
            prefix_start, opset.reshape(prefix_range, output_shape=[1, prefix_len], special_zero=False)
        )
    else:
        y_indices = opset.broadcast(
            data=prefix_range,
            target_shape=opset.concat([batch_slice, as_node([prefix_len])], axis=0),
            broadcast_spec="BIDIRECTIONAL",
        )
        indices = opset.add(indices, index_update_node).output(0)

    y_indices = opset.reshape(y_indices, output_shape=[-1, 1], special_zero=False)
    prefix_indices = opset.concat([x_indices, y_indices], axis=1)
    indices = opset.concat([indices, prefix_indices], axis=0)

    attention_mask = opset.concat(
        [
            opset.broadcast(
                data=make_constant_node(1, dtype=attention_mask.get_element_type()),
                target_shape=opset.concat([batch_slice, as_node([prefix_len])], axis=0),
            ),
            attention_mask,
        ],
        axis=1,
    )
    return dense_shape.output(0), indices.output(0), values.output(0), attention_mask


def get_sp_detokenizer(
    sp_model_node: Node,
    streaming_detokenizer: bool = False,
    clean_up_tokenization_spaces: bool = False,
    prepend_scheme: str = "",
    add_prefix_space: Optional[bool] = None,
) -> Model:
    model_input = token_ids = op.Parameter(Type.i32, PartialShape(["?", "?"]))  # (batch, sequence)

    detokenizer = (
        _get_factory()
        .create(
            "SentencepieceStreamDetokenizer" if streaming_detokenizer else "SentencepieceDetokenizer",
            [sp_model_node, token_ids],
        )
        .outputs()
    )

    if streaming_detokenizer:
        detokenizer = RegexDecodingStep.replace_sp_spaces().get_ov_subgraph(detokenizer)

    if not streaming_detokenizer and prepend_scheme == "always" and add_prefix_space is False:
        detokenizer = RegexDecodingStep.strip_forward_space().get_ov_subgraph(detokenizer)
    elif not streaming_detokenizer and prepend_scheme == "first" and add_prefix_space is False:
        detokenizer = RegexDecodingStep.strip_forward_space_before_not_space().get_ov_subgraph(detokenizer)

    if clean_up_tokenization_spaces:
        detokenizer = RegexDecodingStep.clean_up_tokenization_spaces().get_ov_subgraph(detokenizer)

    string_output = _get_factory().create("StringTensorPack", detokenizer).outputs()
    string_output[0].tensor.add_names({STRING_OUTPUT_NAME})
    tokenizer_detokenizer = Model(string_output, [model_input], DETOKENIZER_NAME)
    tokenizer_detokenizer.validate_nodes_and_infer_types()
    return tokenizer_detokenizer


def is_tiktoken_model(hf_tokenizer: PreTrainedTokenizerBase) -> bool:
    try:
        from tiktoken import Encoding
    except ImportError:
        return False

    return getattr(hf_tokenizer, "vocab_files_names", {}).get("vocab_file", "").endswith(".tiktoken") or isinstance(
        getattr(hf_tokenizer, "encoder", None), Encoding
    )


def convert_tiktoken_model_tokenizer(
    hf_tokenizer: PreTrainedTokenizerBase,
    with_detokenizer: bool = False,
    skip_special_tokens: bool = False,
    clean_up_tokenization_spaces: Optional[bool] = None,
    use_max_padding: bool = False,
) -> Union[Model, Tuple[Model, Model]]:
    encoding = getattr(hf_tokenizer, "tokenizer", None) or hf_tokenizer.encoder
    split_pattern = encoding._pat_str

    pipeline = TokenizerPipeline()
    skip_tokens = []
    if skip_special_tokens:
        skip_tokens = list(parse_special_tokens(hf_tokenizer))

    pipeline.add_steps(
        [
            NormalizeUnicode("NFC"),
            RegexSplitStep(split_pattern, behaviour="contiguous"),
            BytesToCharsStep(),
            BPETokenizationStep.from_tiktoken_encoding(encoding),
            TruncationStep.from_hf_object(hf_tokenizer),
            PaddingStep(
                token=getattr(hf_tokenizer, "pad_token"),
                _token_id=getattr(hf_tokenizer, "pad_token_id"),
                pad_right=(hf_tokenizer.padding_side == "right"),
                pad_to_max_length=use_max_padding,
            ),
            VocabDecoderStep(skip_tokens=skip_tokens),
            CharsToBytesStep(),
        ]
    )
    if clean_up_tokenization_spaces is None:
        clean_up_tokenization_spaces = getattr(hf_tokenizer, "clean_up_tokenization_spaces", None)

    if clean_up_tokenization_spaces:
        pipeline.add_steps(RegexDecodingStep.clean_up_tokenization_spaces())

    pipeline.eos_token_id = pipeline.get_eos_token_id(hf_tokenizer)

    if not with_detokenizer:
        return pipeline.get_tokenizer_ov_subgraph()

    return pipeline.get_tokenizer_ov_subgraph(), pipeline.get_detokenizer_ov_subgraph()
