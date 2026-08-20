import type {
  Model,
  ModelItem,
} from '@/app/components/header/account-setting/model-provider-page/declarations'
import {
  ConfigurationMethodEnum,
  ModelFeatureEnum,
  ModelStatusEnum,
  ModelTypeEnum,
} from '@/app/components/header/account-setting/model-provider-page/declarations'
import { isAgentCompatibleModel, isAgentSuggestedModel } from '../model-compatibility'

const createModel = (provider: string): Model => ({
  provider,
  icon_small: { en_US: '', zh_Hans: '' },
  label: { en_US: provider, zh_Hans: provider },
  models: [],
  status: ModelStatusEnum.active,
})

const createModelItem = (model: string, overrides: Partial<ModelItem> = {}): ModelItem => ({
  model,
  label: { en_US: model, zh_Hans: model },
  model_type: ModelTypeEnum.textGeneration,
  fetch_from: ConfigurationMethodEnum.predefinedModel,
  status: ModelStatusEnum.active,
  model_properties: {},
  load_balancing_enabled: false,
  ...overrides,
})

const createModelItemWithLabel = (
  model: string,
  label: string,
  overrides: Partial<ModelItem> = {},
): ModelItem =>
  createModelItem(model, {
    label: { en_US: label, zh_Hans: label },
    ...overrides,
  })

describe('isAgentCompatibleModel', () => {
  it('should allow models with tool-call capabilities regardless of model name', () => {
    const provider = createModel('any-provider')

    expect(
      isAgentCompatibleModel(
        provider,
        createModelItem('gpt-4o-mini', { features: [ModelFeatureEnum.toolCall] }),
      ),
    ).toBe(true)
    expect(
      isAgentCompatibleModel(
        provider,
        createModelItem('claude-3-haiku-20240307', {
          features: [ModelFeatureEnum.multiToolCall],
        }),
      ),
    ).toBe(true)
    expect(
      isAgentCompatibleModel(
        provider,
        createModelItem('other-model', { features: [ModelFeatureEnum.streamToolCall] }),
      ),
    ).toBe(true)
  })

  it('should allow models without feature metadata', () => {
    const provider = createModel('any-provider')

    expect(isAgentCompatibleModel(provider, createModelItem('gpt-5.5'))).toBe(true)
    expect(isAgentCompatibleModel(provider, createModelItem('other-model', { features: [] }))).toBe(
      true,
    )
  })

  it('should reject models that declare non-tool-call capabilities only', () => {
    const provider = createModel('any-provider')

    expect(
      isAgentCompatibleModel(
        provider,
        createModelItem('vision-model', { features: [ModelFeatureEnum.vision] }),
      ),
    ).toBe(false)
  })

  it('should ignore provider and English label when evaluating compatibility', () => {
    const provider = createModel('any-provider')

    expect(
      isAgentCompatibleModel(
        provider,
        createModelItemWithLabel('model-id', 'gpt-4o', { features: [ModelFeatureEnum.toolCall] }),
      ),
    ).toBe(true)
    expect(
      isAgentCompatibleModel(
        createModel('custom-provider'),
        createModelItemWithLabel('gpt-4o', 'safe-model-label', {
          features: [ModelFeatureEnum.toolCall],
        }),
      ),
    ).toBe(true)
  })
})

describe('isAgentSuggestedModel', () => {
  it('should suggest configured Agent baseline models by English label', () => {
    const provider = createModel('any-provider')

    expect(isAgentSuggestedModel(provider, createModelItem('gpt-5.5'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('gpt-5.5-pro'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('gpt-5.6'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('gpt-5.6-sol'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('gpt-5.6-terra'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('gpt-5.6-luna'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('Claude Opus 4.8'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('opus-4.7'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('Claude Sonnet 4.6'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('Gemini 3.1 Pro Preview'))).toBe(false)
    expect(isAgentSuggestedModel(provider, createModelItem('Gemini 3.1 Pro Preview 001'))).toBe(
      false,
    )
    expect(isAgentSuggestedModel(provider, createModelItem('grok-4.3'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('deepseek-v4-pro'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('kimi-k2.6'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('minimax-m3'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('qwen3.7-max'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('qwen3-coder-plus'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('glm-5.1'))).toBe(true)
    expect(isAgentSuggestedModel(provider, createModelItem('other-model'))).toBe(false)
  })

  it('should evaluate suggestions against the English model label', () => {
    const provider = createModel('any-provider')

    expect(
      isAgentSuggestedModel(provider, createModelItemWithLabel('model-id', 'GPT 5.5 Pro')),
    ).toBe(true)
    expect(
      isAgentSuggestedModel(provider, createModelItemWithLabel('gpt-5.5', 'safe-model-label')),
    ).toBe(false)
  })
})
