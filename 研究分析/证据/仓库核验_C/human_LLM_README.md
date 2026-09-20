# Ontology extension :milky_way: with LLMs :space_invader:

This is the GitLab project for the paper _Ontology Engineering with Large Language Models: Unveiling the potential of human-LLM collaboration in the ontology extension process_ submitted to the [LLM-TEXT2KG 2025](https://aiisc.ai/text2kg2025/): 4th International Workshop on
LLM-Integrated Knowledge Graph Generation from Text (Text2KG), co-located with the [ESWC 2025](https://2025.eswc-conferences.org/). Our work explores the integration of Large Language Models (LLMs) in the process of extending an existing ontology.

## :open_file_folder: Content of the repository
1. [Data gathered through the interviews, including the analysis and results](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/tree/main/Interviews).
2. [Human-LLM collaboration process framework for ontology extension prototype](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/human_LLM_collaboration_framework.pdf).
3. [Prompt templates](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/tree/main/Prompt_Templates).
5. [Demonstration and Evaluation results](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/tree/main/Demonstration_and_Evaluation), including [Inputs](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/tree/main/Demonstration_and_Evaluation/Inputs) and [Outputs](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/tree/main/Demonstration_and_Evaluation/Outputs?ref_type=heads).


### :paperclip: Inventory of files used for the demonstration and evaluation, and for the end-user experiment
- The file [CGO-v2.3 WORKING-VERSION.ttl](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/CGO-v2.3%20WORKING-VERSION.ttl?ref_type=heads) is the Turtle triples file containing the Common Greenhouse Ontology before adding any data for the SENS use case. This file is taken from the CGO repository.
- The file [cgo.txt](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/cgo.txt?ref_type=heads) has the same content as the previous file, but formatted as a markdown code block and saved with the .txt extension so it can be added to the file store of the GPT Assistant used for the demonstration and evaluation of the process framework.
- The file [cgo_manual.pdf](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/cgo_manual.pdf?ref_type=heads) contains some additional information (in natural language) about the CGO. It is used for more context about the CGO in the demonstration and evaluation. It is also added to the file store of the GPT Assistant. This file is taken from the public CGO repository.
- The file [SENS-CGO-extensions.ttl](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/SENS-CGO-extensions.ttl?ref_type=heads) is the Turtle triples file containing the manual extension made for the SENS use case. This is used as the gold standard to evaluate the ontology extension generated with the help of the LLMs (in this case only the GPT Assistant).
- The file [extension_usecase_description.pdf](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/extension_usecase_description.pdf?ref_type=heads) contains the adapted description of the SENS use case for the demonstration and evaluation using the GPT Assistant. This is meant to be used as a starting point to develop the ontology extension. This file can also be used for some of the prompts to give more context to the LLM (in this case the GPT Assistant).
- The file [human_LLM_collaboration_framework.pdf](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/human_LLM_collaboration_framework.pdf?ref_type=heads) is a [flowchart diagram](https://en.wikipedia.org/wiki/Flowchart) that describes the manual approach assited by LLMs developed within this research project. This is a prototype design version. It is in based in X ontology engineering best practices and guidelines, compiled in their internal documentation, and on a series of interviews to 11 experts in ontology engineering and LLMs. This framework or approach aims to facilitate the ontology extension process and to provide guidance to the user of the framework on how to use LLMs for some specific dowsntream tasks.

## :space_invader: GPT Assistant: Common Greenhouse Ontology Expert
We have created a [GPT Assistant](https://platform.openai.com/docs/assistants/overview) using the [OpenAI API Platform](https://platform.openai.com/docs/overview). The GPT Assistant is called "Common Greenhouse Ontology Expert", and its purpose is to assist the ontology engineer with some tasks in the ontology extension process. To that end, we have added in its file store a copy of the CGO ([cgo.txt](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/cgo.txt?ref_type=heads)) and some documentation ([cgo_manual.pdf](https://gitlab.com/eswc2025/ontology-extension-with-llms/-/blob/main/cgo_manual.pdf?ref_type=heads)). We use the recent model [GPT-4o](https://openai.com/index/hello-gpt-4o/) for the GPT Assistant. Again, the role of the LLM is to assist the ontology engineer, but all the tasks are manually performed and reviewed by the human expert (the ontology engineer).

Although there are many other LLMs, both open-source and proprietary that could be used, we have decided to use OpenAI's GPT due to its demonstrated good performance in a wide range of natural language processing (NLP) and ontology engineering (OE) tasks, and its accessibility, especially useful as this case where we are just trying to evaluate a prototype. In the future, specific OE tasks might be performed better by fine-tuned and (hopefully) open-source models. The questions concerning which models and for which tasks are left here as a direction for future research. In addition, the OpenAI API Platform offers a user-friendly interface, which is a requirement in this project, since the manual approach must be executed by users that might not have experience dealing with LLMs, if not via a chat interface.


## :crystal_ball: Future outlook
With the advancements in both the technology and the regulatory landscape, we envision LLMs as powerful tools integrated in the daily ontology engineering processes and within the ontology engineering toolkit. As an example of this tool integration, we see LLMs powering Protégé plugins and Visual Studio Code extension for automatic syntax suggestions or formalization of Competency Questions.

## :sun_with_face: :last_quarter_moon_with_face: Authors and acknowledgment
- Anonymous.

## :books: References
- Amini, R., Norouzi, S. S., Hitzler, P., & Amini, R. (2024). Towards Complex Ontology Alignment using Large Language Models. arXiv preprint arXiv:2404.10329.
- Bakker, R., De Boer, M. H. T. (2024) Dynamic Knowledge Graph Evaluation. TechRxiv. DOI: 10.36227/techrxiv.171779320.04772689/v1.
- Bakker, R., van Drie, R., Bouter, C., van Leeuwen, S., van Rooijen, L., & Top, J. (2021). The Common Greenhouse Ontology: an ontology describing components, properties, and measurements inside the greenhouse. Engineering Proceedings, 9(1), 27.
- Erlingsson, C., & Brysiewicz, P. (2017). A hands-on guide to doing content analysis. African Journal of Emergency Medicine, 7(3), 93–99. https://doi.org/10.1016/j.afjem.2017.08.001
- Fathallah, N., Das, A., De Giorgis, S., Poltronieri, A., Haase, P., & Kovriguina, L. (2024). NeOn-GPT: A Large Language Model-Powered Pipeline for Ontology Learning. Special Track Large Language Models for Knowledge Engineering. European Semantic Web Conference (ESWC) 2024, Hersonissos, Greece.
- Norouzi, S. S., Mahdavinejad, M. S., & Hitzler, P. (2023). Conversational ontology alignment with chatgpt. arXiv preprint arXiv:2308.09217.
- Saeedizade, M. J., & Blomqvist, E. (2024). Navigating Ontology Development with Large Language Models. In European Semantic Web Conference (pp. 143-161). Cham: Springer Nature Switzerland.
- Seljemo, C., Wiig, S., Røise, O., Ellis, L. A., Braithwaite, J., & Ree, E. (2024). How Norwegian homecare managers tackled COVID-19 and displayed resilience-in-action: Multiple perspectives of frontline-staff. Journal of Contingencies and Crisis Management, 32(1), e12558. https://doi.org/10.1111/1468-5973.12558
- Verhoosel, J. P. C., Nouwt, B., Bakker, R. M., Sapounas, A., & Slager, B. (2019). A datahub for semantic interoperability in data-driven integrated greenhouse systems. In Proceedings of the EFITA Conference, Rhodes Island, Griekenland (pp. 7-29).
- Zhang, B., Carriero, V. A., Schreiberhuber, K., Tsaneva, S., González, L. S., Kim, J., & de Berardinis, J. (2024). OntoChat: A Framework for Conversational Ontology Engineering using Language Models. Special Track Large Language Models for Knowledge Engineering. European Semantic Web Conference (ESWC) 2024, Hersonissos, Greece.

## :page_with_curl: License
Ontology extension with LLMs is released under Apache 2.0, for more information see the LICENSE.
