"""Combine many documents together by recursively reducing them."""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    cast,
)

from langchain_core.documents import Document
from langchain_core.language_models import LanguageModelLike
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import BasePromptTemplate
from langchain_core.pydantic_v1 import Extra
from langchain_core.runnables import (
    Runnable,
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)

from langchain.callbacks.manager import Callbacks
from langchain.chains.combine_documents.base import (
    DEFAULT_DOCUMENT_PROMPT,
    DEFAULT_DOCUMENT_SEPARATOR,
    DOCUMENTS_KEY,
    BaseCombineDocumentsChain,
    format_document_inputs,
    format_documents,
    validate_prompt,
)
from langchain.pydantic_v1 import BaseModel

# --- LCEL Runnable chains --- #


def create_collapse_documents_chain(
    llm: LanguageModelLike,
    prompt: BasePromptTemplate,
    *,
    token_max: int,
    document_prompt: Optional[BasePromptTemplate] = None,
    document_separator: str = DEFAULT_DOCUMENT_SEPARATOR,
    token_len_func: Optional[Callable[[str], int]] = None,
) -> Runnable[Dict[str, Any], List[Document]]:
    """Create

    Args:
        llm: Language model to use for collapsing document contents.
        prompt: Prompt to use for collapsing document contents. Must accept "context" as
            one of the input variables. The formatted documents will be passed in as
            "context".
        token_max: The maximum cumulative token length that the list of Documents can
            have.
        document_prompt: Prompt used for formatting each document into a string. Input
            variables can be "page_content" or any metadata keys that are in all
            documents. "page_content" will automatically retrieve the
            `Document.page_content`, and all other inputs variables will be
            automatically retrieved from the `Document.metadata` dictionary. Default to
            a prompt that only contains `Document.page_content`.
        document_separator: Separator string to use for joining formatted document
            strings.
        token_len_func: Optional Callable for computing token length of a string. Should
            take string as input and output an int. If None, will default to
            `llm.get_num_tokens` if the `llm` has this method, otherwise will use `len`.

    Returns:
        An LCEL `Runnable` chain.

        Expects a dictionary as input. Input must contain "context" key with a list of
        Documents.

        Returns a list of Documents whose cumulative token length when formatted as
        strings is below `token_max`.

    Example:
        .. code-block:: python

            # pip install -U langchain langchain-community

            from langchain_community.chat_models import ChatOpenAI
            from langchain_core.documents import Document
            from langchain_core.prompts import ChatPromptTemplate
            from langchain.chains.combine_documents import create_collapse_documents_chain

            llm = ChatOpenAI(model="gpt-3.5-turbo")
            extract_prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", "Given a user question, extract the most relevant parts of the following context:\n\n{context}"),
                    ("human", "{question}"),
                ]
            )
            collapse_documents_chain = create_collapse_documents_chain(llm, extract_prompt, token_max=1000)

            docs = [
                Document(page_content="I love yellow. " * 200),
                Document(page_content="You love green. " * 200),
            ]
            collapse_documents_chain.invoke({"context": docs, "question": "What color do you love?"})
    """  # noqa: E501
    validate_prompt(prompt, (DOCUMENTS_KEY,))
    _document_prompt = document_prompt or DEFAULT_DOCUMENT_PROMPT
    _token_len_func: Callable = token_len_func or getattr(llm, "get_num_tokens", len)  # type: ignore  # noqa: E501

    # Runnable: Dict with many docs -> reduced string.
    reduce_content = (
        RunnableLambda(cast(Callable, format_document_inputs))
        .bind(
            document_prompt=_document_prompt,
            document_separator=document_separator,
        )
        .pipe(prompt, llm, StrOutputParser(), name="reduce_content")
    )

    # Runnable: Dict with many docs -> single Document.
    reduce_docs = RunnableParallel(
        page_content=reduce_content,
        metadata=_combine_metadata,
    ).pipe(_dict_to_document, name="reduce_documents")

    def partition_docs(inputs: dict) -> List[dict]:
        docs = inputs.pop(DOCUMENTS_KEY)
        partitions = split_list_of_docs(
            docs,
            _docs_len,
            token_max,
            token_len_func=_token_len_func,
            document_prompt=_document_prompt,
            document_separator=document_separator,
        )
        return [{DOCUMENTS_KEY: docs, **inputs} for docs in partitions]

    partition_and_reduce = RunnableLambda(partition_docs).pipe(
        reduce_docs.map(), name="partition_and_reduce"
    )

    def collapse_loop(inputs: dict) -> Union[List[Document], Runnable]:
        """Recursive collapse loop.

        If the cumulative token length of the documents exceeds the token_max,
            append another partition and reduce step to the Runnable sequence. Otherwise
            return the docs.
        """
        docs = inputs[DOCUMENTS_KEY]
        curr_len = _docs_len(
            docs, _token_len_func, _document_prompt, document_separator
        )
        if curr_len > token_max:
            return RunnablePassthrough.assign(context=partition_and_reduce).pipe(
                collapse_loop, name="collapse_step"
            )
        else:
            return docs

    return (
        RunnableLambda(cast(Callable, collapse_loop))
        .with_name("collapse_documents_chain")
        .with_types(output_type=_CollapseOutputType)  # type: ignore
    )


# --- Helper methods for LCEL Runnable chains --- #


def _combine_metadata(inputs: Dict[str, Any]) -> Dict[Any, str]:
    docs = inputs[DOCUMENTS_KEY]
    combined_metadata = {k: str(v) for k, v in docs[0].metadata.items()}
    for doc in docs[1:]:
        for k, v in doc.metadata.items():
            if k in combined_metadata:
                combined_metadata[k] += f", {v}"
            else:
                combined_metadata[k] = str(v)
    return combined_metadata


def _dict_to_document(inputs: Dict[str, Any]) -> Document:
    return Document(page_content=inputs["page_content"], metadata=inputs["metadata"])


def _docs_len(
    docs: Sequence[Document],
    token_len_func: Callable[[str], int],
    document_prompt: BasePromptTemplate,
    document_separator: str,
) -> int:
    return token_len_func(format_documents(docs, document_prompt, document_separator))


class _CollapseOutputType(BaseModel):
    __root__: List[Document]


# --- Legacy Chain --- #


class CombineDocsProtocol(Protocol):
    """Interface for the combine_docs method."""

    def __call__(self, docs: List[Document], **kwargs: Any) -> str:
        """Interface for the combine_docs method."""


class AsyncCombineDocsProtocol(Protocol):
    """Interface for the combine_docs method."""

    async def __call__(self, docs: List[Document], **kwargs: Any) -> str:
        """Async interface for the combine_docs method."""


def split_list_of_docs(
    docs: List[Document], length_func: Callable, token_max: int, **kwargs: Any
) -> List[List[Document]]:
    """Split Documents into subsets that each meet a cumulative length constraint.

    Args:
        docs: The full list of Documents.
        length_func: Function for computing the cumulative length of a set of Documents.
        token_max: The maximum cumulative length of any subset of Documents.
        **kwargs: Arbitrary additional keyword params to pass to each call of the
            length_func.

    Returns:
        A List[List[Document]].
    """
    new_result_doc_list = []
    _sub_result_docs = []
    for doc in docs:
        _sub_result_docs.append(doc)
        _num_tokens = length_func(_sub_result_docs, **kwargs)
        if _num_tokens > token_max:
            if len(_sub_result_docs) == 1:
                raise ValueError(
                    "A single document was longer than the context length,"
                    " we cannot handle this."
                )
            new_result_doc_list.append(_sub_result_docs[:-1])
            _sub_result_docs = _sub_result_docs[-1:]
    new_result_doc_list.append(_sub_result_docs)
    return new_result_doc_list


def collapse_docs(
    docs: List[Document],
    combine_document_func: CombineDocsProtocol,
    **kwargs: Any,
) -> Document:
    """Execute a collapse function on a set of documents and merge their metadatas.

    Args:
        docs: A list of Documents to combine.
        combine_document_func: A function that takes in a list of Documents and
            optionally addition keyword parameters and combines them into a single
            string.
        **kwargs: Arbitrary additional keyword params to pass to the
            combine_document_func.

    Returns:
        A single Document with the output of combine_document_func for the page content
            and the combined metadata's of all the input documents. All metadata values
            are strings, and where there are overlapping keys across documents the
            values are joined by ", ".
    """
    result = combine_document_func(docs, **kwargs)
    combined_metadata = {k: str(v) for k, v in docs[0].metadata.items()}
    for doc in docs[1:]:
        for k, v in doc.metadata.items():
            if k in combined_metadata:
                combined_metadata[k] += f", {v}"
            else:
                combined_metadata[k] = str(v)
    return Document(page_content=result, metadata=combined_metadata)


async def acollapse_docs(
    docs: List[Document],
    combine_document_func: AsyncCombineDocsProtocol,
    **kwargs: Any,
) -> Document:
    """Execute a collapse function on a set of documents and merge their metadatas.

    Args:
        docs: A list of Documents to combine.
        combine_document_func: A function that takes in a list of Documents and
            optionally addition keyword parameters and combines them into a single
            string.
        **kwargs: Arbitrary additional keyword params to pass to the
            combine_document_func.

    Returns:
        A single Document with the output of combine_document_func for the page content
            and the combined metadata's of all the input documents. All metadata values
            are strings, and where there are overlapping keys across documents the
            values are joined by ", ".
    """
    result = await combine_document_func(docs, **kwargs)
    combined_metadata = {k: str(v) for k, v in docs[0].metadata.items()}
    for doc in docs[1:]:
        for k, v in doc.metadata.items():
            if k in combined_metadata:
                combined_metadata[k] += f", {v}"
            else:
                combined_metadata[k] = str(v)
    return Document(page_content=result, metadata=combined_metadata)


class ReduceDocumentsChain(BaseCombineDocumentsChain):
    """Combine documents by recursively reducing them.

    This involves

    - combine_documents_chain

    - collapse_documents_chain

    `combine_documents_chain` is ALWAYS provided. This is final chain that is called.
    We pass all previous results to this chain, and the output of this chain is
    returned as a final result.

    `collapse_documents_chain` is used if the documents passed in are too many to all
    be passed to `combine_documents_chain` in one go. In this case,
    `collapse_documents_chain` is called recursively on as big of groups of documents
    as are allowed.

    Example:
        .. code-block:: python

            from langchain.chains import (
                StuffDocumentsChain, LLMChain, ReduceDocumentsChain
            )
            from langchain_core.prompts import PromptTemplate
            from langchain.llms import OpenAI

            # This controls how each document will be formatted. Specifically,
            # it will be passed to `format_document` - see that function for more
            # details.
            document_prompt = PromptTemplate(
                input_variables=["page_content"],
                 template="{page_content}"
            )
            document_variable_name = "context"
            llm = OpenAI()
            # The prompt here should take as an input variable the
            # `document_variable_name`
            prompt = PromptTemplate.from_template(
                "Summarize this content: {context}"
            )
            llm_chain = LLMChain(llm=llm, prompt=prompt)
            combine_documents_chain = StuffDocumentsChain(
                llm_chain=llm_chain,
                document_prompt=document_prompt,
                document_variable_name=document_variable_name
            )
            chain = ReduceDocumentsChain(
                combine_documents_chain=combine_documents_chain,
            )
            # If we wanted to, we could also pass in collapse_documents_chain
            # which is specifically aimed at collapsing documents BEFORE
            # the final call.
            prompt = PromptTemplate.from_template(
                "Collapse this content: {context}"
            )
            llm_chain = LLMChain(llm=llm, prompt=prompt)
            collapse_documents_chain = StuffDocumentsChain(
                llm_chain=llm_chain,
                document_prompt=document_prompt,
                document_variable_name=document_variable_name
            )
            chain = ReduceDocumentsChain(
                combine_documents_chain=combine_documents_chain,
                collapse_documents_chain=collapse_documents_chain,
            )
    """

    combine_documents_chain: BaseCombineDocumentsChain
    """Final chain to call to combine documents.
    This is typically a StuffDocumentsChain."""
    collapse_documents_chain: Optional[BaseCombineDocumentsChain] = None
    """Chain to use to collapse documents if needed until they can all fit.
    If None, will use the combine_documents_chain.
    This is typically a StuffDocumentsChain."""
    token_max: int = 3000
    """The maximum number of tokens to group documents into. For example, if
    set to 3000 then documents will be grouped into chunks of no greater than
    3000 tokens before trying to combine them into a smaller chunk."""

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        arbitrary_types_allowed = True

    @property
    def _collapse_chain(self) -> BaseCombineDocumentsChain:
        if self.collapse_documents_chain is not None:
            return self.collapse_documents_chain
        else:
            return self.combine_documents_chain

    def combine_docs(
        self,
        docs: List[Document],
        token_max: Optional[int] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Tuple[str, dict]:
        """Combine multiple documents recursively.

        Args:
            docs: List of documents to combine, assumed that each one is less than
                `token_max`.
            token_max: Recursively creates groups of documents less than this number
                of tokens.
            callbacks: Callbacks to be passed through
            **kwargs: additional parameters to be passed to LLM calls (like other
                input variables besides the documents)

        Returns:
            The first element returned is the single string output. The second
            element returned is a dictionary of other keys to return.
        """
        result_docs, extra_return_dict = self._collapse(
            docs, token_max=token_max, callbacks=callbacks, **kwargs
        )
        return self.combine_documents_chain.combine_docs(
            docs=result_docs, callbacks=callbacks, **kwargs
        )

    async def acombine_docs(
        self,
        docs: List[Document],
        token_max: Optional[int] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Tuple[str, dict]:
        """Async combine multiple documents recursively.

        Args:
            docs: List of documents to combine, assumed that each one is less than
                `token_max`.
            token_max: Recursively creates groups of documents less than this number
                of tokens.
            callbacks: Callbacks to be passed through
            **kwargs: additional parameters to be passed to LLM calls (like other
                input variables besides the documents)

        Returns:
            The first element returned is the single string output. The second
            element returned is a dictionary of other keys to return.
        """
        result_docs, extra_return_dict = await self._acollapse(
            docs, token_max=token_max, callbacks=callbacks, **kwargs
        )
        return await self.combine_documents_chain.acombine_docs(
            docs=result_docs, callbacks=callbacks, **kwargs
        )

    def _collapse(
        self,
        docs: List[Document],
        token_max: Optional[int] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Tuple[List[Document], dict]:
        result_docs = docs
        length_func = self.combine_documents_chain.prompt_length
        num_tokens = length_func(result_docs, **kwargs)

        def _collapse_docs_func(docs: List[Document], **kwargs: Any) -> str:
            return self._collapse_chain.run(
                input_documents=docs, callbacks=callbacks, **kwargs
            )

        _token_max = token_max or self.token_max
        while num_tokens is not None and num_tokens > _token_max:
            new_result_doc_list = split_list_of_docs(
                result_docs, length_func, _token_max, **kwargs
            )
            result_docs = []
            for docs in new_result_doc_list:
                new_doc = collapse_docs(docs, _collapse_docs_func, **kwargs)
                result_docs.append(new_doc)
            num_tokens = length_func(result_docs, **kwargs)
        return result_docs, {}

    async def _acollapse(
        self,
        docs: List[Document],
        token_max: Optional[int] = None,
        callbacks: Callbacks = None,
        **kwargs: Any,
    ) -> Tuple[List[Document], dict]:
        result_docs = docs
        length_func = self.combine_documents_chain.prompt_length
        num_tokens = length_func(result_docs, **kwargs)

        async def _collapse_docs_func(docs: List[Document], **kwargs: Any) -> str:
            return await self._collapse_chain.arun(
                input_documents=docs, callbacks=callbacks, **kwargs
            )

        _token_max = token_max or self.token_max
        while num_tokens is not None and num_tokens > _token_max:
            new_result_doc_list = split_list_of_docs(
                result_docs, length_func, _token_max, **kwargs
            )
            result_docs = []
            for docs in new_result_doc_list:
                new_doc = await acollapse_docs(docs, _collapse_docs_func, **kwargs)
                result_docs.append(new_doc)
            num_tokens = length_func(result_docs, **kwargs)
        return result_docs, {}

    @property
    def _chain_type(self) -> str:
        return "reduce_documents_chain"
