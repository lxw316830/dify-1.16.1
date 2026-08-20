import type {
  Model,
  ModelItem,
} from '@/app/components/header/account-setting/model-provider-page/declarations'
import { supportFunctionCall } from '@/utils/tool-call'

const agentSuggestedModelPatterns: RegExp[] = [
  // openai
  /^gpt[ .-]5\.5$/i,
  /^gpt[ .-]5\.5[ .-]pro$/i,
  /^gpt[ .-]5\.6(?:[ .-](?:sol|terra|luna))?$/i,

  // anthropic
  /^(?:claude[ .-])?opus[ .-]4\.8$/i,
  /^(?:claude[ .-])?opus[ .-]4\.7$/i,
  /^(?:claude[ .-])?sonnet[ .-]4\.6$/i,

  // The Gemini model plugin quality is not strong enough yet, so do not recommend it for now.
  // /^gemini 3\.1 pro preview$/i,

  // x
  /^grok[ .-]4\.3$/i,

  // deepseek
  /^deepseek[ .-]v4[ .-]pro$/i,

  // qwen
  /^qwen[ .-]?3\.7[ .-]max$/i,
  /^qwen[ .-]?3[ .-]coder[ .-]plus$/i,

  // moonshot
  /^kimi[ .-]k2\.6$/i,

  // minimax
  /^minimax[ .-]m3$/i,

  // zhipuai
  /^glm[ .-]5\.1$/i,
]

export function isAgentCompatibleModel(_provider: Model, modelItem: ModelItem) {
  return !modelItem.features?.length || supportFunctionCall(modelItem.features)
}

export function isAgentSuggestedModel(_provider: Model, modelItem: ModelItem) {
  return agentSuggestedModelPatterns.some((pattern) => pattern.test(modelItem.label.en_US))
}
