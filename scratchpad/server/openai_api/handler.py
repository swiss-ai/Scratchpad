# adapted from https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/openai_api/adapter.py
"""
Copyright 2023-2024 SGLang Team
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import time
import json
import uuid
import base64
import asyncio
from http import HTTPStatus
from typing import Dict, List

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from outlines.integrations.utils import convert_json_schema_to_str

from .conversation import (
    Conversation,
    SeparatorStyle,
    chat_template_exists,
    generate_chat_conv,
    register_conv_template,
)
from scratchpad.managers.structs import EmbeddingReqInput, GenerateReqInput
from scratchpad.constrained.func_calls import TOOLS_TAG_LIST, parse_tool_response
from .protocol import (
    BatchRequest,
    BatchResponse,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseChoice,
    ChatCompletionResponseStreamChoice,
    ChatCompletionStreamResponse,
    ChatCompletionTokenLogprob,
    ChatMessage,
    ChoiceLogprobs,
    CompletionRequest,
    CompletionResponse,
    CompletionResponseChoice,
    CompletionResponseStreamChoice,
    CompletionStreamResponse,
    DeltaMessage,
    EmbeddingObject,
    EmbeddingRequest,
    EmbeddingResponse,
    ErrorResponse,
    FileDeleteResponse,
    FileRequest,
    FileResponse,
    LogProbs,
    TopLogprob,
    UsageInfo,
    ToolCall,
    FunctionResponse,
)
from scratchpad.server.controller import model_to_topping
from scratchpad.utils import logger
from .reasoning_parser import ReasoningParser
from .function_call_parser import FunctionCallParser

chat_template_name = None


class FileMetadata:
    def __init__(self, filename: str, purpose: str):
        self.filename = filename
        self.purpose = purpose


batch_storage: Dict[str, BatchResponse] = {}
file_id_request: Dict[str, FileMetadata] = {}
file_id_response: Dict[str, FileResponse] = {}
file_id_storage: Dict[str, str] = {}

storage_dir = None


def create_error_response(
    message: str,
    err_type: str = "BadRequestError",
    status_code: HTTPStatus = HTTPStatus.BAD_REQUEST,
):
    error = ErrorResponse(message=message, type=err_type, code=status_code.value)
    return JSONResponse(content=error.model_dump(), status_code=error.code)


def create_streaming_error_response(
    message: str,
    err_type: str = "BadRequestError",
    status_code: HTTPStatus = HTTPStatus.BAD_REQUEST,
) -> str:
    error = ErrorResponse(message=message, type=err_type, code=status_code.value)
    json_str = json.dumps({"error": error.model_dump()})
    return json_str


def load_chat_template_for_openai_api(tokenizer_manager, chat_template_arg):
    global chat_template_name

    logger.info(f"Use chat template: {chat_template_arg}")
    if not chat_template_exists(chat_template_arg):
        if not os.path.exists(chat_template_arg):
            raise RuntimeError(
                f"Chat template {chat_template_arg} is not a built-in template name "
                "or a valid chat template file path."
            )
        if chat_template_arg.endswith(".jinja"):
            with open(chat_template_arg, "r") as f:
                chat_template = "".join(f.readlines()).strip("\n")
            tokenizer_manager.tokenizer.chat_template = chat_template.replace(
                "\\n", "\n"
            )
            chat_template_name = None
        else:
            assert chat_template_arg.endswith(
                ".json"
            ), "unrecognized format of chat template file"
            with open(chat_template_arg, "r") as filep:
                template = json.load(filep)
                try:
                    sep_style = SeparatorStyle[template["sep_style"]]
                except KeyError:
                    raise ValueError(
                        f"Unknown separator style: {template['sep_style']}"
                    ) from None
                register_conv_template(
                    Conversation(
                        name=template["name"],
                        system_template=template["system"] + "\n{system_message}",
                        system_message=template.get("system_message", ""),
                        roles=(template["user"], template["assistant"]),
                        sep_style=sep_style,
                        sep=template.get("sep", "\n"),
                        stop_str=template["stop_str"],
                    ),
                    override=True,
                )
            chat_template_name = template["name"]
    else:
        chat_template_name = chat_template_arg


async def v1_files_create(file: UploadFile, purpose: str, file_storage_pth: str = None):
    try:
        global storage_dir
        if file_storage_pth:
            storage_dir = file_storage_pth
        # Read the file content
        file_content = await file.read()

        # Create an instance of RequestBody
        request_body = FileRequest(file=file_content, purpose=purpose)

        # Save the file to the sglang_oai_storage directory
        os.makedirs(storage_dir, exist_ok=True)
        file_id = f"backend_input_file-{uuid.uuid4()}"
        filename = f"{file_id}.jsonl"
        file_path = os.path.join(storage_dir, filename)

        with open(file_path, "wb") as f:
            f.write(request_body.file)

        # add info to global file map
        file_id_request[file_id] = FileMetadata(filename=file.filename, purpose=purpose)
        file_id_storage[file_id] = file_path

        # Return the response in the required format
        response = FileResponse(
            id=file_id,
            bytes=len(request_body.file),
            created_at=int(time.time()),
            filename=file.filename,
            purpose=request_body.purpose,
        )
        file_id_response[file_id] = response

        return response
    except ValidationError as e:
        return {"error": "Invalid input", "details": e.errors()}


async def v1_delete_file(file_id: str):
    # Retrieve the file job from the in-memory storage
    file_response = file_id_response.get(file_id)
    if file_response is None:
        raise HTTPException(status_code=404, detail="File not found")
    file_path = file_id_storage.get(file_id)
    if file_path is None:
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(file_path)
    del file_id_response[file_id]
    del file_id_storage[file_id]
    return FileDeleteResponse(id=file_id, deleted=True)


async def v1_batches(tokenizer_manager, raw_request: Request):
    try:
        body = await raw_request.json()

        batch_request = BatchRequest(**body)

        batch_id = f"batch_{uuid.uuid4()}"

        # Create an instance of BatchResponse
        batch_response = BatchResponse(
            id=batch_id,
            endpoint=batch_request.endpoint,
            input_file_id=batch_request.input_file_id,
            completion_window=batch_request.completion_window,
            created_at=int(time.time()),
            metadata=batch_request.metadata,
        )

        batch_storage[batch_id] = batch_response

        # Start processing the batch asynchronously
        asyncio.create_task(process_batch(tokenizer_manager, batch_id, batch_request))

        # Return the initial batch_response
        return batch_response

    except ValidationError as e:
        return {"error": "Invalid input", "details": e.errors()}
    except Exception as e:
        return {"error": str(e)}


async def process_batch(tokenizer_manager, batch_id: str, batch_request: BatchRequest):
    try:
        # Update the batch status to "in_progress"
        batch_storage[batch_id].status = "in_progress"
        batch_storage[batch_id].in_progress_at = int(time.time())

        # Retrieve the input file content
        input_file_request = file_id_request.get(batch_request.input_file_id)
        if not input_file_request:
            raise ValueError("Input file not found")

        # Parse the JSONL file and process each request
        input_file_path = file_id_storage.get(batch_request.input_file_id)
        with open(input_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total_requests = len(lines)
        completed_requests = 0
        failed_requests = 0

        all_ret = []
        end_point = batch_storage[batch_id].endpoint
        file_request_list = []
        all_requests = []
        request_ids = []
        for line in lines:
            request_data = json.loads(line)
            file_request_list.append(request_data)
            body = request_data["body"]
            request_ids.append(request_data["custom_id"])

            # Although streaming is supported for standalone completions, it is not supported in
            # batch mode (multiple completions in single request).
            if body.get("stream", False):
                raise ValueError("Streaming requests are not supported in batch mode")

            if end_point == "/v1/chat/completions":
                all_requests.append(ChatCompletionRequest(**body))
            elif end_point == "/v1/completions":
                all_requests.append(CompletionRequest(**body))

        if end_point == "/v1/chat/completions":
            adapted_request, request = v1_chat_generate_request(
                all_requests, tokenizer_manager, request_ids=request_ids
            )
        elif end_point == "/v1/completions":
            adapted_request, request = v1_generate_request(
                all_requests, request_ids=request_ids
            )

        try:
            ret = await tokenizer_manager.generate_request(adapted_request).__anext__()
            if not isinstance(ret, list):
                ret = [ret]
            if end_point == "/v1/chat/completions":
                responses = v1_chat_generate_response(request, ret, to_file=True)
            else:
                responses = v1_generate_response(
                    request, ret, tokenizer_manager, to_file=True
                )

        except Exception as e:
            error_json = {
                "id": f"batch_req_{uuid.uuid4()}",
                "custom_id": request_data.get("custom_id"),
                "response": None,
                "error": {"message": str(e)},
            }
            all_ret.append(error_json)
            failed_requests += len(file_request_list)

        for idx, response in enumerate(responses):
            # the batch_req here can be changed to be named within a batch granularity
            response_json = {
                "id": f"batch_req_{uuid.uuid4()}",
                "custom_id": file_request_list[idx].get("custom_id"),
                "response": response,
                "error": None,
            }
            all_ret.append(response_json)
            completed_requests += 1

        # Write results to a new file
        output_file_id = f"backend_result_file-{uuid.uuid4()}"
        global storage_dir
        output_file_path = os.path.join(storage_dir, f"{output_file_id}.jsonl")
        with open(output_file_path, "w", encoding="utf-8") as f:
            for ret in all_ret:
                f.write(json.dumps(ret) + "\n")

        # Update batch response with output file information
        retrieve_batch = batch_storage[batch_id]
        retrieve_batch.output_file_id = output_file_id
        file_id_storage[output_file_id] = output_file_path
        file_id_response[output_file_id] = FileResponse(
            id=output_file_id,
            bytes=os.path.getsize(output_file_path),
            created_at=int(time.time()),
            filename=f"{output_file_id}.jsonl",
            purpose="batch_result",
        )
        # Update batch status to "completed"
        retrieve_batch.status = "completed"
        retrieve_batch.completed_at = int(time.time())
        retrieve_batch.request_counts = {
            "total": total_requests,
            "completed": completed_requests,
            "failed": failed_requests,
        }

    except Exception as e:
        logger.error("error in SP:", e)
        # Update batch status to "failed"
        retrieve_batch = batch_storage[batch_id]
        retrieve_batch.status = "failed"
        retrieve_batch.failed_at = int(time.time())
        retrieve_batch.errors = {"message": str(e)}


async def v1_retrieve_batch(batch_id: str):
    # Retrieve the batch job from the in-memory storage
    batch_response = batch_storage.get(batch_id)
    if batch_response is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    return batch_response


async def v1_cancel_batch(tokenizer_manager, batch_id: str):
    # Retrieve the batch job from the in-memory storage
    batch_response = batch_storage.get(batch_id)
    if batch_response is None:
        raise HTTPException(status_code=404, detail="Batch not found")

    # Only do cancal when status is "validating" or "in_progress"
    if batch_response.status in ["validating", "in_progress"]:
        # Start cancelling the batch asynchronously
        asyncio.create_task(
            cancel_batch(
                tokenizer_manager=tokenizer_manager,
                batch_id=batch_id,
                input_file_id=batch_response.input_file_id,
            )
        )

        # Update batch status to "cancelling"
        batch_response.status = "cancelling"

        return batch_response
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Current status is {batch_response.status}, no need to cancel",
        )


async def cancel_batch(tokenizer_manager, batch_id: str, input_file_id: str):
    try:
        # Update the batch status to "cancelling"
        batch_storage[batch_id].status = "cancelling"

        # Retrieve the input file content
        input_file_request = file_id_request.get(input_file_id)
        if not input_file_request:
            raise ValueError("Input file not found")

        # Parse the JSONL file and process each request
        input_file_path = file_id_storage.get(input_file_id)
        with open(input_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        file_request_list = []
        request_ids = []
        for line in lines:
            request_data = json.loads(line)
            file_request_list.append(request_data)
            request_ids.append(request_data["custom_id"])

        # Cancel requests by request_ids
        for rid in request_ids:
            tokenizer_manager.abort_request(rid=rid)

        retrieve_batch = batch_storage[batch_id]
        retrieve_batch.status = "cancelled"

    except Exception as e:
        logger.error("error in SP:", e)
        # Update batch status to "failed"
        retrieve_batch = batch_storage[batch_id]
        retrieve_batch.status = "failed"
        retrieve_batch.failed_at = int(time.time())
        retrieve_batch.errors = {"message": str(e)}


async def v1_retrieve_file(file_id: str):
    # Retrieve the batch job from the in-memory storage
    file_response = file_id_response.get(file_id)
    if file_response is None:
        raise HTTPException(status_code=404, detail="File not found")
    return file_response


async def v1_retrieve_file_content(file_id: str):
    file_pth = file_id_storage.get(file_id)
    if not file_pth or not os.path.exists(file_pth):
        raise HTTPException(status_code=404, detail="File not found")

    def iter_file():
        with open(file_pth, mode="rb") as file_like:
            yield from file_like

    return StreamingResponse(iter_file(), media_type="application/octet-stream")


def v1_generate_request(
    all_requests: List[CompletionRequest], request_ids: List[str] = None
):
    prompts = []
    sampling_params_list = []
    return_logprobs = []
    logprob_start_lens = []
    top_logprobs_nums = []
    topping_paths = []
    # NOTE: with openai API, the prompt's logprobs are always not computed
    first_prompt_type = type(all_requests[0].prompt)
    for request in all_requests:
        assert (
            type(request.prompt) is first_prompt_type
        ), "All prompts must be of the same type in file input settings"
        if len(all_requests) > 1 and request.n > 1:
            raise ValueError(
                "Parallel sampling is not supported for completions from files"
            )

    for request in all_requests:
        prompts.append(request.prompt)
        return_logprobs.append(request.logprobs is not None and request.logprobs > 0)
        logprob_start_lens.append(-1)
        top_logprobs_nums.append(
            request.logprobs if request.logprobs is not None else 0
        )
        topping_paths.append(model_to_topping(request.model))
        sampling_params_list.append(
            {
                "temperature": request.temperature,
                "max_new_tokens": request.max_tokens,
                "min_new_tokens": request.min_tokens,
                "stop": request.stop,
                "stop_token_ids": request.stop_token_ids,
                "top_p": request.top_p,
                "presence_penalty": request.presence_penalty,
                "frequency_penalty": request.frequency_penalty,
                "repetition_penalty": request.repetition_penalty,
                "regex": request.regex,
                "json_schema": request.json_schema,
                "n": request.n,
                "ignore_eos": request.ignore_eos,
            }
        )

    if len(all_requests) == 1:
        prompt = prompts[0]
        sampling_params_list = sampling_params_list[0]
        logprob_start_lens = logprob_start_lens[0]
        return_logprobs = return_logprobs[0]
        top_logprobs_nums = top_logprobs_nums[0]
        if isinstance(prompt, str) or isinstance(prompt[0], str):
            prompt_kwargs = {"text": prompt}
        else:
            prompt_kwargs = {"input_ids": prompt}
        topping_paths = topping_paths[0]
    else:
        if isinstance(prompts[0], str):
            prompt_kwargs = {"text": prompts}
        else:
            prompt_kwargs = {"input_ids": prompts}

    adapted_request = GenerateReqInput(
        **prompt_kwargs,
        sampling_params=sampling_params_list,
        return_logprob=return_logprobs,
        top_logprobs_num=top_logprobs_nums,
        logprob_start_len=logprob_start_lens,
        return_text_in_logprobs=True,
        stream=all_requests[0].stream,
        rid=request_ids,
        topping_path=topping_paths,
    )

    if len(all_requests) == 1:
        return adapted_request, all_requests[0]
    return adapted_request, all_requests


def v1_generate_response(request, ret, tokenizer_manager, to_file=False):
    choices = []
    echo = False
    topping_paths = []
    if (not isinstance(request, list)) and request.echo:
        # TODO: handle the case propmt is token ids
        if isinstance(request.prompt, list) and isinstance(request.prompt[0], str):
            # for the case of multiple str prompts
            prompts = request.prompt
        elif isinstance(request.prompt, list) and isinstance(request.prompt[0], list):
            # for the case of multiple token ids prompts
            prompts = [
                tokenizer_manager.tokenizer.decode(prompt, skip_special_tokens=True)
                for prompt in request.prompt
            ]
        elif isinstance(request.prompt, list) and isinstance(request.prompt[0], int):
            # for the case of single token ids prompt
            prompts = [
                tokenizer_manager.tokenizer.decode(
                    request.prompt, skip_special_tokens=True
                )
            ]
        else:
            # for the case of single str prompt
            prompts = [request.prompt]
        echo = True

    for idx, ret_item in enumerate(ret):
        text = ret_item["text"]
        if isinstance(request, list) and request[idx].echo:
            echo = True
            text = request[idx].prompt + text
        if (not isinstance(request, list)) and echo:
            prompt_index = idx // request.n
            text = prompts[prompt_index] + text

        logprobs = False
        if isinstance(request, list) and request[idx].logprobs:
            logprobs = True
        elif (not isinstance(request, list)) and request.logprobs:
            logprobs = True
        if logprobs:
            if echo:
                input_token_logprobs = ret_item["meta_info"]["input_token_logprobs"]
                input_top_logprobs = ret_item["meta_info"]["input_top_logprobs"]
            else:
                input_token_logprobs = None
                input_top_logprobs = None

            logprobs = to_openai_style_logprobs(
                input_token_logprobs=input_token_logprobs,
                input_top_logprobs=input_top_logprobs,
                output_token_logprobs=ret_item["meta_info"]["output_token_logprobs"],
                output_top_logprobs=ret_item["meta_info"]["output_top_logprobs"],
            )
        else:
            logprobs = None

        if to_file:
            # to make the choise data json serializable
            choice_data = {
                "index": 0,
                "text": text,
                "logprobs": logprobs,
                "finish_reason": (
                    ret_item["meta_info"]["finish_reason"]["type"]
                    if ret_item["meta_info"]["finish_reason"]
                    else ""
                ),
            }
        else:
            choice_data = CompletionResponseChoice(
                index=idx,
                text=text,
                logprobs=logprobs,
                finish_reason=(
                    ret_item["meta_info"]["finish_reason"]["type"]
                    if ret_item["meta_info"]["finish_reason"]
                    else ""
                ),
            )

        choices.append(choice_data)

    if to_file:
        responses = []
        for i, choice in enumerate(choices):
            response = {
                "status_code": 200,
                "request_id": ret[i]["meta_info"]["id"],
                "body": {
                    # remain the same but if needed we can change that
                    "id": ret[i]["meta_info"]["id"],
                    "object": "text_completion",
                    "created": int(time.time()),
                    "model": request[i].model,
                    "choices": choice,
                    "usage": {
                        "prompt_tokens": ret[i]["meta_info"]["prompt_tokens"],
                        "completion_tokens": ret[i]["meta_info"]["completion_tokens"],
                        "total_tokens": ret[i]["meta_info"]["prompt_tokens"]
                        + ret[i]["meta_info"]["completion_tokens"],
                    },
                    "system_fingerprint": None,
                },
            }
            responses.append(response)
        return responses
    else:
        prompt_tokens = sum(
            ret[i]["meta_info"]["prompt_tokens"] for i in range(0, len(ret), request.n)
        )
        completion_tokens = sum(item["meta_info"]["completion_tokens"] for item in ret)
        response = CompletionResponse(
            id=ret[0]["meta_info"]["id"],
            model=request.model,
            choices=choices,
            usage=UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
    return response


async def v1_completions(tokenizer_manager, raw_request: Request):
    request_json = await raw_request.json()
    all_requests = [CompletionRequest(**request_json)]
    adapted_request, request = v1_generate_request(all_requests)

    if adapted_request.stream:

        async def generate_stream_resp():
            stream_buffers = {}
            n_prev_tokens = {}
            prompt_tokens = {}
            completion_tokens = {}
            try:
                async for content in tokenizer_manager.generate_request(
                    adapted_request, raw_request
                ):
                    index = content.get("index", 0)

                    stream_buffer = stream_buffers.get(index, "")
                    n_prev_token = n_prev_tokens.get(index, 0)

                    text = content["text"]
                    prompt_tokens[index] = content["meta_info"]["prompt_tokens"]
                    completion_tokens[index] = content["meta_info"]["completion_tokens"]

                    if not stream_buffer:  # The first chunk
                        if request.echo:
                            if isinstance(request.prompt, str):
                                # for the case of single str prompts
                                prompts = request.prompt
                            elif isinstance(request.prompt, list):
                                if isinstance(request.prompt[0], str):
                                    # for the case of multiple str prompts
                                    prompts = request.prompt[index // request.n]
                                elif isinstance(request.prompt[0], int):
                                    # for the case of single token ids prompt
                                    prompts = tokenizer_manager.tokenizer.decode(
                                        request.prompt, skip_special_tokens=True
                                    )
                                elif isinstance(request.prompt[0], list) and isinstance(
                                    request.prompt[0][0], int
                                ):
                                    # for the case of multiple token ids prompts
                                    prompts = tokenizer_manager.tokenizer.decode(
                                        request.prompt[index // request.n],
                                        skip_special_tokens=True,
                                    )

                            # Prepend prompt in response text.
                            text = prompts + text

                    if request.logprobs:
                        # The first chunk and echo is enabled.
                        if not stream_buffer and request.echo:
                            input_token_logprobs = content["meta_info"][
                                "input_token_logprobs"
                            ]
                            input_top_logprobs = content["meta_info"][
                                "input_top_logprobs"
                            ]
                        else:
                            input_token_logprobs = None
                            input_top_logprobs = None

                        logprobs = to_openai_style_logprobs(
                            input_token_logprobs=input_token_logprobs,
                            input_top_logprobs=input_top_logprobs,
                            output_token_logprobs=content["meta_info"][
                                "output_token_logprobs"
                            ][n_prev_token:],
                            output_top_logprobs=content["meta_info"][
                                "output_top_logprobs"
                            ][n_prev_token:],
                        )
                        n_prev_token = len(
                            content["meta_info"]["output_token_logprobs"]
                        )
                    else:
                        logprobs = None

                    delta = text[len(stream_buffer) :]
                    stream_buffer = stream_buffer + delta
                    choice_data = CompletionResponseStreamChoice(
                        index=index,
                        text=delta,
                        logprobs=logprobs,
                        finish_reason=(
                            content["meta_info"]["finish_reason"]["type"]
                            if content["meta_info"]["finish_reason"]
                            else ""
                        ),
                    )
                    chunk = CompletionStreamResponse(
                        id=content["meta_info"]["id"],
                        object="text_completion",
                        choices=[choice_data],
                        model=request.model,
                    )

                    stream_buffers[index] = stream_buffer
                    n_prev_tokens[index] = n_prev_token

                    yield f"data: {chunk.model_dump_json()}\n\n"
                if request.stream_options and request.stream_options.include_usage:
                    total_prompt_tokens = sum(
                        tokens
                        for i, tokens in prompt_tokens.items()
                        if i % request.n == 0
                    )
                    total_completion_tokens = sum(
                        tokens for tokens in completion_tokens.values()
                    )
                    usage = UsageInfo(
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        total_tokens=total_prompt_tokens + total_completion_tokens,
                    )

                    final_usage_chunk = CompletionStreamResponse(
                        id=str(uuid.uuid4().hex),
                        choices=[],
                        model=request.model,
                        usage=usage,
                    )
                    final_usage_data = final_usage_chunk.model_dump_json(
                        exclude_unset=True, exclude_none=True
                    )
                    yield f"data: {final_usage_data}\n\n"
            except ValueError as e:
                error = create_streaming_error_response(str(e))
                yield f"data: {error}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate_stream_resp(),
            media_type="text/event-stream",
            background=tokenizer_manager.create_abort_task(adapted_request),
        )

    # Non-streaming response.
    try:
        ret = await tokenizer_manager.generate_request(
            adapted_request, raw_request
        ).__anext__()
    except ValueError as e:
        return create_error_response(str(e))

    if not isinstance(ret, list):
        ret = [ret]

    response = v1_generate_response(request, ret, tokenizer_manager)
    return response


def _get_enable_thinking_from_request(request_obj):
    """Extracts the 'enable_thinking' flag from request chat_template_kwargs.

    Args:
        request_obj: The request object (or an item from a list of requests).

    Returns:
        The boolean value of 'enable_thinking' if found and not True, otherwise True.
    """
    if (
        hasattr(request_obj, "chat_template_kwargs")
        and request_obj.chat_template_kwargs
        and request_obj.chat_template_kwargs.get("enable_thinking") is not None
    ):
        return request_obj.chat_template_kwargs.get("enable_thinking")
    return True


def v1_chat_generate_request(
    all_requests: List[ChatCompletionRequest],
    tokenizer_manager,
    request_ids: List[str] = None,
):
    input_ids = []
    sampling_params_list = []
    image_data_list = []
    return_logprobs = []
    logprob_start_lens = []
    top_logprobs_nums = []
    modalities_list = []
    topping_paths = []
    # NOTE: with openai API, the prompt's logprobs are always not computed

    for request in all_requests:
        # Prep the data needed for the underlying GenerateReqInput:
        #  - prompt: The full prompt string.
        #  - stop: Custom stop tokens.
        #  - image_data: None or a list of image strings (URLs or base64 strings).
        #    None skips any image processing in GenerateReqInput.
        if not isinstance(request.messages, str):
            tools = None
            if request.tools and request.tool_choice != "none":
                request.skip_special_tokens = False
                if request.stream:
                    logger.warning("Streaming is not supported with tools.")
                    request.stream = False
                if not isinstance(request.tool_choice, str):
                    tools = [
                        item.function.model_dump()
                        for item in request.tools
                        if item.function.name == request.tool_choice.function.name
                    ]
                else:
                    tools = [item.function.model_dump() for item in request.tools]

            # Apply chat template and its stop strings.
            if chat_template_name is None:
                openai_compatible_messages = []
                for message in request.messages:
                    if isinstance(message.content, str):
                        openai_compatible_messages.append(
                            {"role": message.role, "content": message.content}
                        )
                    else:
                        content_list = message.dict()["content"]
                        for content in content_list:
                            if content["type"] == "text":
                                openai_compatible_messages.append(
                                    {"role": message.role, "content": content["text"]}
                                )
                if openai_compatible_messages[-1]["role"] == "assistant":
                    assistant_prefix = openai_compatible_messages[-1]["content"]
                    openai_compatible_messages = openai_compatible_messages[:-1]
                else:
                    assistant_prefix = None
                prompt_ids = tokenizer_manager.tokenizer.apply_chat_template(
                    openai_compatible_messages,
                    tokenize=True,
                    add_generation_prompt=True,
                    tools=tools,
                )
                templated_message = tokenizer_manager.tokenizer.apply_chat_template(
                    openai_compatible_messages,
                    tokenize=False,
                    add_generation_prompt=True,
                    tools=tools,
                )
                if assistant_prefix:
                    prompt_ids += tokenizer_manager.tokenizer.encode(assistant_prefix)
                stop = request.stop
                image_data = None
                modalities = []
            else:
                conv = generate_chat_conv(request, chat_template_name)
                prompt = conv.get_prompt()
                image_data = conv.image_data
                modalities = conv.modalities
                stop = conv.stop_str or []
                if request.stop:
                    if isinstance(request.stop, str):
                        stop.append(request.stop)
                    else:
                        stop.extend(request.stop)
                prompt_ids = tokenizer_manager.tokenizer.encode(prompt)
        else:
            # Use the raw prompt and stop strings if the messages is already a string.
            prompt_ids = request.messages
            stop = request.stop
            image_data = None
            modalities = []
        input_ids.append(prompt_ids)
        return_logprobs.append(request.logprobs)
        logprob_start_lens.append(-1)
        top_logprobs_nums.append(request.top_logprobs or 0)
        topping_paths.append(model_to_topping(request.model))
        sampling_params = {
            "temperature": request.temperature,
            "max_new_tokens": request.max_tokens,
            "min_new_tokens": request.min_tokens,
            "stop": stop,
            "stop_token_ids": request.stop_token_ids,
            "top_p": request.top_p,
            "presence_penalty": request.presence_penalty,
            "frequency_penalty": request.frequency_penalty,
            "repetition_penalty": request.repetition_penalty,
            "regex": request.regex,
            "n": request.n,
        }
        if request.response_format and request.response_format.type == "json_schema":
            sampling_params["json_schema"] = convert_json_schema_to_str(
                request.response_format.json_schema.schema_
            )
        sampling_params_list.append(sampling_params)
        image_data_list.append(image_data)
        modalities_list.extend(modalities)

    if len(all_requests) == 1:
        input_ids = input_ids[0]
        if isinstance(input_ids, str):
            prompt_kwargs = {"text": input_ids}
        else:
            prompt_kwargs = {"input_ids": input_ids}
        sampling_params_list = sampling_params_list[0]
        image_data = image_data_list[0]
        return_logprobs = return_logprobs[0]
        logprob_start_lens = logprob_start_lens[0]
        top_logprobs_nums = top_logprobs_nums[0]
        modalities_list = modalities_list[:1]
        topping_paths = topping_paths[0]
    else:
        if isinstance(input_ids[0], str):
            prompt_kwargs = {"text": input_ids}
        else:
            prompt_kwargs = {"input_ids": input_ids}
    adapted_request = GenerateReqInput(
        **prompt_kwargs,
        image_data=image_data,
        sampling_params=sampling_params_list,
        return_logprob=return_logprobs,
        logprob_start_len=logprob_start_lens,
        top_logprobs_num=top_logprobs_nums,
        stream=all_requests[0].stream,
        return_text_in_logprobs=True,
        rid=request_ids,
        modalities=modalities_list,
        topping_path=topping_paths,
    )
    if len(all_requests) == 1:
        return adapted_request, all_requests[0]
    return adapted_request, all_requests


def v1_chat_generate_response(
    request,
    ret,
    to_file=False,
    cache_report=False,
    tool_call_parser=None,
    reasoning_parser=None,
):
    choices = []
    for idx, ret_item in enumerate(ret):
        logprobs = False
        if isinstance(request, list) and request[idx].logprobs:
            logprobs = True
        elif (not isinstance(request, list)) and request.logprobs:
            logprobs = True
        if logprobs:
            logprobs = to_openai_style_logprobs(
                output_token_logprobs=ret_item["meta_info"]["output_token_logprobs"],
                output_top_logprobs=ret_item["meta_info"].get(
                    "output_top_logprobs", None
                ),
            )
            token_logprobs = []
            for token_idx, (token, logprob) in enumerate(
                zip(logprobs.tokens, logprobs.token_logprobs)
            ):
                token_bytes = list(token.encode("utf-8"))
                top_logprobs = []
                if logprobs.top_logprobs:
                    for top_token, top_logprob in logprobs.top_logprobs[
                        token_idx
                    ].items():
                        top_token_bytes = list(top_token.encode("utf-8"))
                        top_logprobs.append(
                            TopLogprob(
                                token=top_token,
                                bytes=top_token_bytes,
                                logprob=top_logprob,
                            )
                        )
                token_logprobs.append(
                    ChatCompletionTokenLogprob(
                        token=token,
                        bytes=token_bytes,
                        logprob=logprob,
                        top_logprobs=top_logprobs,
                    )
                )
            choice_logprobs: ChoiceLogprobs = ChoiceLogprobs(content=token_logprobs)
        else:
            choice_logprobs = None

        finish_reason = ret_item["meta_info"]["finish_reason"]

        tool_calls = None
        text = ret_item["text"]

        if isinstance(request, list):
            tool_choice = request[idx].tool_choice
            tools = request[idx].tools
            separate_reasoning = request[idx].separate_reasoning
            enable_thinking = _get_enable_thinking_from_request(request[idx])
        else:
            tool_choice = request.tool_choice
            tools = request.tools
            separate_reasoning = request.separate_reasoning
            enable_thinking = _get_enable_thinking_from_request(request)
        reasoning_text = None
        if reasoning_parser and separate_reasoning and enable_thinking:
            try:
                parser = ReasoningParser(
                    model_type=reasoning_parser, stream_reasoning=False
                )
                reasoning_text, text = parser.parse_non_stream(text)
            except Exception as e:
                logger.error(f"Exception: {e}")
                return create_error_response(
                    HTTPStatus.BAD_REQUEST,
                    "Failed to parse reasoning related info to json format!",
                )
        if tool_choice != "none" and tools:

            parser = FunctionCallParser(tools, tool_call_parser)
            if parser.has_tool_call(text):
                if finish_reason["type"] == "stop":
                    finish_reason["type"] = "tool_calls"
                    finish_reason["matched"] = None
                try:
                    text, call_info_list = parser.parse_non_stream(text)
                    tool_calls = [
                        ToolCall(
                            id=f"call_{base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b'=').decode()}",
                            index=call_info.tool_index,
                            function=FunctionResponse(
                                name=call_info.name, arguments=call_info.parameters
                            ),
                        )
                        for call_info in call_info_list
                    ]
                except Exception as e:
                    logger.error(f"Exception: {e}")
                    return create_error_response(
                        HTTPStatus.BAD_REQUEST,
                        "Failed to parse fc related info to json format!",
                    )

        choice_data = ChatCompletionResponseChoice(
            index=idx,
            message=ChatMessage(
                role="assistant",
                content=text if text else None,
                tool_calls=tool_calls,
                reasoning_content=reasoning_text if reasoning_text else None,
            ),
            logprobs=choice_logprobs,
            finish_reason=finish_reason["type"] if finish_reason else None,
            matched_stop=(
                finish_reason["matched"]
                if finish_reason and "matched" in finish_reason
                else None
            ),
        )
        choices.append(choice_data)
    prompt_tokens = sum(
        ret[i]["meta_info"]["prompt_tokens"] for i in range(0, len(ret), request.n)
    )
    completion_tokens = sum(item["meta_info"]["completion_tokens"] for item in ret)
    cached_tokens = sum(item["meta_info"].get("cached_tokens", 0) for item in ret)
    response = ChatCompletionResponse(
        id=ret[0]["meta_info"]["id"],
        model=request.model,
        choices=choices,
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens_details=(
                {"cached_tokens": cached_tokens} if cache_report else None
            ),
        ),
    )
    return response


async def v1_chat_completions(
    tokenizer_manager, raw_request: Request, cache_report=False
):
    try:
        request_json = await raw_request.json()
    except Exception as e:
        return create_error_response("Invalid request body, error: ", str(e))
    all_requests = [ChatCompletionRequest(**request_json)]
    created = int(time.time())
    adapted_request, request = v1_chat_generate_request(all_requests, tokenizer_manager)

    if adapted_request.stream:
        parser_dict = {}
        reasoning_parser_dict = {}

        async def generate_stream_resp():
            tool_call_first = True
            is_firsts = {}
            stream_buffers = {}
            n_prev_tokens = {}
            prompt_tokens = {}
            completion_tokens = {}
            cached_tokens = {}
            try:
                async for content in tokenizer_manager.generate_request(
                    adapted_request, raw_request
                ):
                    index = content.get("index", 0)
                    text = content["text"]

                    is_first = is_firsts.get(index, True)
                    stream_buffer = stream_buffers.get(index, "")
                    n_prev_token = n_prev_tokens.get(index, 0)

                    prompt_tokens[index] = content["meta_info"]["prompt_tokens"]
                    completion_tokens[index] = content["meta_info"]["completion_tokens"]
                    cached_tokens[index] = content["meta_info"].get("cached_tokens", 0)
                    if request.logprobs:
                        logprobs = to_openai_style_logprobs(
                            output_token_logprobs=content["meta_info"][
                                "output_token_logprobs"
                            ][n_prev_token:],
                            output_top_logprobs=content["meta_info"].get(
                                "output_top_logprobs", []
                            )[n_prev_token:],
                        )

                        n_prev_token = len(
                            content["meta_info"]["output_token_logprobs"]
                        )
                        token_logprobs = []
                        for token, logprob in zip(
                            logprobs.tokens, logprobs.token_logprobs
                        ):
                            token_bytes = list(token.encode("utf-8"))
                            top_logprobs = []
                            if logprobs.top_logprobs:
                                for top_token, top_logprob in logprobs.top_logprobs[
                                    0
                                ].items():
                                    top_token_bytes = list(top_token.encode("utf-8"))
                                    top_logprobs.append(
                                        TopLogprob(
                                            token=top_token,
                                            bytes=top_token_bytes,
                                            logprob=top_logprob,
                                        )
                                    )
                            token_logprobs.append(
                                ChatCompletionTokenLogprob(
                                    token=token,
                                    bytes=token_bytes,
                                    logprob=logprob,
                                    top_logprobs=top_logprobs,
                                )
                            )

                        choice_logprobs = ChoiceLogprobs(content=token_logprobs)

                    else:
                        choice_logprobs = None

                    finish_reason = content["meta_info"]["finish_reason"]
                    finish_reason_type = (
                        finish_reason["type"] if finish_reason else None
                    )

                    if is_first:
                        # First chunk with role
                        is_first = False
                        delta = DeltaMessage(role="assistant")
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=index,
                            delta=delta,
                            finish_reason=finish_reason_type,
                            matched_stop=(
                                finish_reason["matched"]
                                if finish_reason and "matched" in finish_reason
                                else None
                            ),
                            logprobs=choice_logprobs,
                        )
                        chunk = ChatCompletionStreamResponse(
                            id=content["meta_info"]["id"],
                            created=created,
                            choices=[choice_data],
                            model=request.model,
                        )
                        yield f"data: {chunk.model_dump_json()}\n\n"

                    text = content["text"]
                    delta = text[len(stream_buffer) :]
                    new_stream_buffer = stream_buffer + delta

                    enable_thinking = _get_enable_thinking_from_request(request)

                    if (
                        tokenizer_manager.server_args.reasoning_parser
                        and request.separate_reasoning
                        and enable_thinking
                    ):
                        if index not in reasoning_parser_dict:
                            reasoning_parser_dict[index] = ReasoningParser(
                                tokenizer_manager.server_args.reasoning_parser,
                                request.stream_reasoning,
                            )
                        reasoning_parser = reasoning_parser_dict[index]
                        reasoning_text, delta = reasoning_parser.parse_stream_chunk(
                            delta
                        )
                        if reasoning_text:
                            choice_data = ChatCompletionResponseStreamChoice(
                                index=index,
                                delta=DeltaMessage(
                                    reasoning_content=(
                                        reasoning_text if reasoning_text else None
                                    )
                                ),
                                finish_reason=finish_reason_type,
                            )
                            chunk = ChatCompletionStreamResponse(
                                id=content["meta_info"]["id"],
                                created=created,
                                choices=[choice_data],
                                model=request.model,
                            )
                            yield f"data: {chunk.model_dump_json()}\n\n"
                        if (delta and len(delta) == 0) or not delta:
                            stream_buffers[index] = new_stream_buffer
                            is_firsts[index] = is_first
                            continue

                    if request.tool_choice != "none" and request.tools:
                        if index not in parser_dict:
                            parser_dict[index] = FunctionCallParser(
                                tools=request.tools,
                                tool_call_parser=tokenizer_manager.server_args.tool_call_parser,
                            )
                        parser = parser_dict[index]

                        # parse_increment => returns (normal_text, calls)
                        normal_text, calls = parser.parse_stream_chunk(delta)

                        # 1) if there's normal_text, output it as normal content
                        if normal_text:
                            choice_data = ChatCompletionResponseStreamChoice(
                                index=index,
                                delta=DeltaMessage(
                                    content=normal_text if normal_text else None
                                ),
                                finish_reason=finish_reason_type,
                            )
                            chunk = ChatCompletionStreamResponse(
                                id=content["meta_info"]["id"],
                                created=created,
                                choices=[choice_data],
                                model=request.model,
                            )
                            yield f"data: {chunk.model_dump_json()}\n\n"

                        # 2) if we found calls, we output them as separate chunk(s)
                        for call_item in calls:
                            # transform call_item -> FunctionResponse + ToolCall
                            if finish_reason_type == "stop":
                                latest_delta_len = 0
                                if isinstance(call_item.parameters, str):
                                    latest_delta_len = len(call_item.parameters)

                                expected_call = json.dumps(
                                    parser.multi_format_parser.detectors[0]
                                    .prev_tool_call_arr[index]
                                    .get("arguments", {}),
                                    ensure_ascii=False,
                                )
                                actual_call = parser.multi_format_parser.detectors[
                                    0
                                ].streamed_args_for_tool[index]
                                if latest_delta_len > 0:
                                    actual_call = actual_call[:-latest_delta_len]
                                remaining_call = expected_call.replace(
                                    actual_call, "", 1
                                )
                                call_item.parameters = remaining_call

                                finish_reason_type = "tool_calls"
                            tool_call = ToolCall(
                                id=(
                                    f"call_{base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b'=').decode()}"
                                    if tool_call_first
                                    else None
                                ),
                                index=call_item.tool_index,
                                function=FunctionResponse(
                                    name=call_item.name,
                                    arguments=call_item.parameters,
                                ),
                            )
                            tool_call_first = False
                            choice_data = ChatCompletionResponseStreamChoice(
                                index=index,
                                delta=DeltaMessage(tool_calls=[tool_call]),
                                finish_reason=(
                                    None
                                    if request.stream_options
                                    and request.stream_options.include_usage
                                    else finish_reason_type
                                ),  # additional chunk will be return
                            )
                            chunk = ChatCompletionStreamResponse(
                                id=content["meta_info"]["id"],
                                created=created,
                                choices=[choice_data],
                                model=request.model,
                            )
                            yield f"data: {chunk.model_dump_json()}\n\n"

                        stream_buffers[index] = new_stream_buffer
                        is_firsts[index] = is_first

                    else:
                        # No tool calls => just treat this as normal text
                        if delta or not (
                            request.stream_options
                            and request.stream_options.include_usage
                        ):
                            choice_data = ChatCompletionResponseStreamChoice(
                                index=index,
                                delta=DeltaMessage(content=delta if delta else None),
                                finish_reason=(
                                    None
                                    if request.stream_options
                                    and request.stream_options.include_usage
                                    else finish_reason_type
                                ),
                                matched_stop=(
                                    finish_reason["matched"]
                                    if finish_reason and "matched" in finish_reason
                                    else None
                                ),
                                logprobs=choice_logprobs,
                            )
                            chunk = ChatCompletionStreamResponse(
                                id=content["meta_info"]["id"],
                                created=created,
                                choices=[choice_data],
                                model=request.model,
                            )
                            yield f"data: {chunk.model_dump_json()}\n\n"
                            stream_buffers[index] = new_stream_buffer
                            is_firsts[index] = is_first
                if finish_reason_type == "stop" and request.tool_choice != "none":
                    parser = FunctionCallParser(
                        tools=request.tools,
                        tool_call_parser=tokenizer_manager.server_args.tool_call_parser,
                    )
                    if parser.has_tool_call(new_stream_buffer):
                        # if the stream ends with empty string after tool calls
                        finish_reason_type = "tool_calls"

                if request.stream_options and request.stream_options.include_usage:
                    total_prompt_tokens = sum(
                        tokens
                        for i, tokens in prompt_tokens.items()
                        if i % request.n == 0
                    )
                    total_completion_tokens = sum(
                        tokens for tokens in completion_tokens.values()
                    )
                    cache_report = tokenizer_manager.server_args.enable_cache_report
                    if cache_report:
                        cached_tokens_sum = sum(
                            tokens for tokens in cached_tokens.values()
                        )
                        prompt_tokens_details = {"cached_tokens": cached_tokens_sum}
                    else:
                        prompt_tokens_details = None
                    usage = UsageInfo(
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        total_tokens=total_prompt_tokens + total_completion_tokens,
                        prompt_tokens_details=prompt_tokens_details,
                    )

                else:
                    usage = None
                final_usage_chunk = ChatCompletionStreamResponse(
                    id=content["meta_info"]["id"],
                    created=created,
                    choices=[
                        ChatCompletionResponseStreamChoice(
                            index=index,
                            delta=DeltaMessage(),
                            finish_reason=finish_reason_type,
                        )
                    ],
                    model=request.model,
                    usage=usage,
                )
                yield f"data: {final_usage_chunk.model_dump_json()}\n\n"
            except ValueError as e:
                error = create_streaming_error_response(str(e))
                yield f"data: {error}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate_stream_resp(),
            media_type="text/event-stream",
            background=tokenizer_manager.create_abort_task(adapted_request),
        )

    # Non-streaming response.
    try:
        ret = await tokenizer_manager.generate_request(
            adapted_request, raw_request
        ).__anext__()
    except ValueError as e:
        return create_error_response(str(e))
    if not isinstance(ret, list):
        ret = [ret]

    response = v1_chat_generate_response(
        request,
        ret,
        created,
        cache_report=tokenizer_manager.server_args.enable_cache_report,
        tool_call_parser=tokenizer_manager.server_args.tool_call_parser,
        reasoning_parser=tokenizer_manager.server_args.reasoning_parser,
    )

    return response


def v1_embedding_request(all_requests, tokenizer_manager):
    prompts = []
    sampling_params_list = []
    first_prompt_type = type(all_requests[0].input)

    for request in all_requests:
        prompt = request.input
        assert (
            type(prompt) == first_prompt_type
        ), "All prompts must be of the same type in file input settings"
        prompts.append(prompt)

    if len(all_requests) == 1:
        prompt = prompts[0]
        if isinstance(prompt, str) or isinstance(prompt[0], str):
            prompt_kwargs = {"text": prompt}
        else:
            prompt_kwargs = {"input_ids": prompt}
    else:
        if isinstance(prompts[0], str) or isinstance(prompts[0][0], str):
            prompt_kwargs = {"text": prompts}
        else:
            prompt_kwargs = {"input_ids": prompts}

    adapted_request = EmbeddingReqInput(
        **prompt_kwargs,
    )

    if len(all_requests) == 1:
        return adapted_request, all_requests[0]
    return adapted_request, all_requests


def v1_embedding_response(ret, model_path, to_file=False):
    embedding_objects = []
    prompt_tokens = 0
    for idx, ret_item in enumerate(ret):
        embedding_objects.append(
            EmbeddingObject(
                embedding=ret[idx]["embedding"],
                index=idx,
            )
        )
        prompt_tokens += ret[idx]["meta_info"]["prompt_tokens"]

    return EmbeddingResponse(
        data=embedding_objects,
        model=model_path,
        usage=UsageInfo(
            prompt_tokens=prompt_tokens,
            total_tokens=prompt_tokens,
        ),
    )


async def v1_embeddings(tokenizer_manager, raw_request: Request):
    request_json = await raw_request.json()
    all_requests = [EmbeddingRequest(**request_json)]
    adapted_request, request = v1_embedding_request(all_requests, tokenizer_manager)

    try:
        ret = await tokenizer_manager.generate_request(
            adapted_request, raw_request
        ).__anext__()
    except ValueError as e:
        return create_error_response(str(e))

    if not isinstance(ret, list):
        ret = [ret]

    response = v1_embedding_response(ret, tokenizer_manager.model_path)

    return response


def to_openai_style_logprobs(
    input_token_logprobs=None,
    output_token_logprobs=None,
    input_top_logprobs=None,
    output_top_logprobs=None,
):
    ret_logprobs = LogProbs()

    def append_token_logprobs(token_logprobs):
        for logprob, _, token_text in token_logprobs:
            ret_logprobs.tokens.append(token_text)
            ret_logprobs.token_logprobs.append(logprob)

            # Not supported yet
            ret_logprobs.text_offset.append(-1)

    def append_top_logprobs(top_logprobs):
        for tokens in top_logprobs:
            if tokens is not None:
                ret_logprobs.top_logprobs.append(
                    {token[2]: token[0] for token in tokens}
                )
            else:
                ret_logprobs.top_logprobs.append(None)

    if input_token_logprobs is not None:
        append_token_logprobs(input_token_logprobs)
    if output_token_logprobs is not None:
        append_token_logprobs(output_token_logprobs)
    if input_top_logprobs is not None:
        append_top_logprobs(input_top_logprobs)
    if output_top_logprobs is not None:
        append_top_logprobs(output_top_logprobs)

    return ret_logprobs
