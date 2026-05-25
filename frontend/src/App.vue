<script setup lang="ts">
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

type Provider = 'openai' | 'claude'
type ComposerMode = 'chat' | 'image'

interface UploadImage {
  name: string
  type: string
  data: string
  preview: string
}

interface Models {
  openai: string
  claude: string
  image: string
}

type ApiKeys = Record<Provider, string>
type ChatModels = Record<Provider, string[]>
type ModelTestStatus = 'unknown' | 'testing' | 'ok' | 'bad'

interface ModelTestResult {
  status: ModelTestStatus
  message?: string
  latencyMs?: number
}

interface Conversation {
  id: string
  title: string
  provider: Provider
  model: string
  createdAt: number
  updatedAt: number
}

interface Message {
  id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  imageUrl?: string | null
  createdAt?: number
}

const configured = ref(false)
const provider = ref<Provider>('openai')
const apiKey = ref('')
const setupError = ref('')
const settingsOpen = ref(false)
const settingsProvider = ref<Provider>('openai')
const settingsApiKey = ref('')
const settingsError = ref('')
const savedApiKeys = ref<ApiKeys>({ openai: '', claude: '' })
const titleDialogOpen = ref(false)
const titleDraft = ref('')
const titleError = ref('')
const editingConversationId = ref<string | null>(null)
const editingConversationTitle = ref('')
const statusText = ref('正在检查设备配置...')
const models = ref<Models>({ openai: 'gpt-5.5', claude: 'claude-sonnet-4-6', image: 'gpt-image-2' })
const availableModels = ref<ChatModels>({ openai: ['gpt-5.5'], claude: ['claude-sonnet-4-6'] })
const modelTests = ref<Record<Provider, Record<string, ModelTestResult>>>({ openai: {}, claude: {} })
const modelLoading = ref(false)
const modelError = ref('')
const conversations = ref<Conversation[]>([])
const activeConversationId = ref<string | null>(null)
const messages = ref<Message[]>([])
const messageCache = ref<Record<string, Message[]>>({})
const pendingConversations = ref<Record<string, boolean>>({})
const input = ref('')
const composerMode = ref<ComposerMode>('chat')
const uploadImages = ref<UploadImage[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const loading = ref(false)
const messagesEl = ref<HTMLElement | null>(null)
const composerTextarea = ref<HTMLTextAreaElement | null>(null)
const previewImageUrl = ref('')
let activeRequestController: AbortController | null = null
let activeRequestConversationId: string | null = null
const cancelledRequestIds = new Set<string>()

function isDeviceToken(value: string | null) {
  return Boolean(value && /^device_[a-f0-9]{32}\.[a-f0-9]{64}$/.test(value))
}

async function getDeviceToken() {
  const stored = localStorage.getItem('easychat_device_token')
  if (isDeviceToken(stored)) return stored as string
  localStorage.removeItem('easychat_device_id')
  const response = await fetch('/api/device')
  const data = await response.json()
  if (!response.ok || !isDeviceToken(data.deviceToken)) {
    throw new Error(data.error || '设备初始化失败')
  }
  localStorage.setItem('easychat_device_token', data.deviceToken)
  return data.deviceToken as string
}

let deviceToken = ''

async function ensureDeviceToken() {
  if (!deviceToken) {
    deviceToken = await getDeviceToken()
  }
  return deviceToken
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = await ensureDeviceToken()
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Device-Token': token,
      ...(options.headers || {})
    }
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data.error || '请求失败')
  }
  return data
}

const activeConversation = computed(() => conversations.value.find((item) => item.id === activeConversationId.value))
const providerModel = computed(() => (provider.value === 'openai' ? models.value.openai : models.value.claude))
const chatModelOptions = computed(() => availableModels.value[provider.value] || [providerModel.value])
const activeModelTest = computed(() => modelTests.value[provider.value]?.[providerModel.value] || { status: 'unknown' as ModelTestStatus })
const activeSavedApiKey = computed(() => savedApiKeys.value[settingsProvider.value] || '')
const canGenerateImage = computed(() => configured.value && provider.value === 'openai')
const activeConversationPending = computed(() => Boolean(activeConversationId.value && pendingConversations.value[activeConversationId.value]))
const sendDisabled = computed(() =>
  activeConversationPending.value ||
  !input.value.trim() ||
  (composerMode.value === 'image' && !canGenerateImage.value) ||
  (composerMode.value === 'chat' && ['testing', 'bad'].includes(activeModelTest.value.status))
)
const inputPlaceholder = computed(() =>
  composerMode.value === 'image'
    ? '描述要生成或修改的图片，Enter 发送'
    : '输入消息，可上传图片，Enter 发送'
)

function providerName(value: Provider) {
  return value === 'openai' ? 'OpenAI' : 'Claude'
}

function modelStorageKey(value: Provider) {
  return `easychat_model_${value}`
}

function setProviderModel(value: Provider, model: string) {
  const nextModel = model.trim()
  if (!nextModel) return
  models.value = { ...models.value, [value]: nextModel }
  localStorage.setItem(modelStorageKey(value), nextModel)
}

function setModelTest(value: Provider, model: string, result: ModelTestResult) {
  modelTests.value = {
    ...modelTests.value,
    [value]: {
      ...modelTests.value[value],
      [model]: result
    }
  }
}

function syncSelectedModel(value: Provider) {
  const options = availableModels.value[value] || []
  const saved = localStorage.getItem(modelStorageKey(value)) || ''
  const current = models.value[value]
  const next = options.includes(saved) ? saved : options.includes(current) ? current : options[0] || current
  setProviderModel(value, next)
}

function modelStatusLabel(value: Provider, model: string) {
  const status = modelTests.value[value]?.[model]?.status || 'unknown'
  if (status === 'ok') return '可用'
  if (status === 'bad') return '不可用'
  if (status === 'testing') return '检测中'
  return '未测'
}

function activeModelStatusText() {
  const result = activeModelTest.value
  if (result.status === 'ok') return `模型可用${result.latencyMs ? ` · ${Math.round(result.latencyMs / 100) / 10}s` : ''}`
  if (result.status === 'bad') return result.message || '模型不可用'
  if (result.status === 'testing') return '正在测试模型'
  return '模型尚未测试'
}

async function testChatModel(value: Provider, model: string) {
  if (!configured.value || !model || modelTests.value[value]?.[model]?.status === 'testing') return
  setModelTest(value, model, { status: 'testing' })
  try {
    const data = await api<{ ok: boolean; error?: string; latencyMs?: number }>(`/api/models/test`, {
      method: 'POST',
      body: JSON.stringify({ provider: value, model })
    })
    setModelTest(value, model, data.ok ? { status: 'ok', latencyMs: data.latencyMs } : { status: 'bad', message: data.error || '模型不可用' })
  } catch (error) {
    setModelTest(value, model, { status: 'bad', message: error instanceof Error ? error.message : '模型测试失败' })
  }
}

async function testProviderModels(value: Provider) {
  await Promise.all((availableModels.value[value] || []).map((model) => testChatModel(value, model)))
}

async function selectChatModel(event: Event) {
  const nextModel = (event.target as HTMLSelectElement).value
  setProviderModel(provider.value, nextModel)
  await testChatModel(provider.value, nextModel)
}

async function refreshAvailableModels() {
  if (!configured.value) return
  modelLoading.value = true
  modelError.value = ''
  try {
    const data = await api<{ models: Partial<ChatModels>; errors?: Partial<Record<Provider, string>> }>('/api/models')
    const openaiModels = data.models.openai?.length ? data.models.openai : [models.value.openai]
    const claudeModels = data.models.claude?.length ? data.models.claude : [models.value.claude]
    availableModels.value = { openai: openaiModels, claude: claudeModels }
    syncSelectedModel('openai')
    syncSelectedModel('claude')
    const errors = data.errors || {}
    modelError.value = [errors.openai && `OpenAI: ${errors.openai}`, errors.claude && `Claude: ${errors.claude}`].filter(Boolean).join('；')
    void testProviderModels(provider.value)
  } catch (error) {
    modelError.value = error instanceof Error ? error.message : '模型列表刷新失败'
  } finally {
    modelLoading.value = false
  }
}

function formatTime(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

function renderMarkdown(content: string) {
  const masked = (content || '').replace(/sk-[A-Za-z0-9_-]{12,}/g, 'sk-***')
  return DOMPurify.sanitize(marked.parse(masked) as string)
}

function referenceMarkdown(images: UploadImage[]) {
  if (!images.length) return ''
  return ['', '', ...images.map((image, index) => `![上传图片 ${index + 1}](${image.preview})`)].join('\n')
}

function openImagePreview(url: string) {
  previewImageUrl.value = url
}

function closeImagePreview() {
  previewImageUrl.value = ''
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === 'AbortError'
}

function stopCurrentRequest() {
  if (activeRequestController) {
    if (activeRequestConversationId) {
      cancelledRequestIds.add(activeRequestConversationId)
      const last = messageCache.value[activeRequestConversationId]?.[messageCache.value[activeRequestConversationId].length - 1]
      if (last?.role === 'assistant' && !last.content && !last.imageUrl) {
        removeCachedLastMessage(activeRequestConversationId)
      }
    }
    activeRequestController.abort()
  }
}

function finishActiveRequest(controller: AbortController, conversationId: string) {
  if (activeRequestController === controller) {
    activeRequestController = null
    activeRequestConversationId = null
  }
  setConversationPending(conversationId, false)
}

function isRequestCancelled(conversationId: string) {
  return cancelledRequestIds.has(conversationId)
}

async function downloadImage(url: string) {
  try {
    const response = await fetch(url)
    if (!response.ok) throw new Error('download failed')
    const blob = await response.blob()
    const objectUrl = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = objectUrl
    link.download = `easychat-image-${Date.now()}.png`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(objectUrl)
  } catch {
    const link = document.createElement('a')
    link.href = url
    link.download = `easychat-image-${Date.now()}.png`
    link.target = '_blank'
    link.click()
  }
}

async function scrollToBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

async function resizeComposer() {
  await nextTick()
  const textarea = composerTextarea.value
  if (!textarea) return
  const maxHeight = 180
  textarea.style.height = 'auto'
  const nextHeight = Math.min(textarea.scrollHeight, maxHeight)
  textarea.style.height = `${nextHeight}px`
  textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden'
}

function setCachedMessages(conversationId: string, nextMessages: Message[]) {
  messageCache.value = { ...messageCache.value, [conversationId]: nextMessages }
  if (activeConversationId.value === conversationId) {
    messages.value = nextMessages
  }
}

function appendCachedMessages(conversationId: string, nextMessages: Message[]) {
  setCachedMessages(conversationId, [...(messageCache.value[conversationId] || []), ...nextMessages])
}

function updateCachedLastAssistant(conversationId: string, updater: (message: Message) => void) {
  const cached = [...(messageCache.value[conversationId] || [])]
  const last = cached[cached.length - 1]
  if (last?.role === 'assistant') {
    updater(last)
    setCachedMessages(conversationId, cached)
  }
}

function removeCachedLastMessage(conversationId: string) {
  const cached = [...(messageCache.value[conversationId] || [])]
  cached.pop()
  setCachedMessages(conversationId, cached)
}

function setConversationPending(conversationId: string, pending: boolean) {
  pendingConversations.value = { ...pendingConversations.value, [conversationId]: pending }
}

function updateConversationInList(updated: Conversation) {
  const found = conversations.value.some((item) => item.id === updated.id)
  conversations.value = found
    ? conversations.value.map((item) => (item.id === updated.id ? { ...item, ...updated } : item))
    : [updated, ...conversations.value]
}

async function boot() {
  loading.value = true
  try {
    await ensureDeviceToken()
    const me = await api<{ configured: boolean; provider: Provider | null; apiKeys: ApiKeys; macAddress: string | null; models: Models }>('/api/me')
    configured.value = me.configured
    models.value = me.models
    availableModels.value = { openai: [me.models.openai], claude: [me.models.claude] }
    savedApiKeys.value = { openai: me.apiKeys?.openai || '', claude: me.apiKeys?.claude || '' }
    if (me.provider) provider.value = me.provider
    syncSelectedModel('openai')
    syncSelectedModel('claude')
    statusText.value = me.configured
      ? `已绑定 ${providerName(provider.value)}，${me.macAddress ? `MAC ${me.macAddress}` : '使用设备 ID 识别'}`
      : '请选择服务商并粘贴 API Key'
    if (configured.value) {
      await refreshAvailableModels()
      await refreshConversations()
      if (conversations.value.length) {
        await openConversation(conversations.value[0].id)
      }
    }
  } catch (error) {
    setupError.value = error instanceof Error ? error.message : '初始化失败'
  } finally {
    loading.value = false
  }
}

function replaceCachedConversationId(fromId: string, toId: string) {
  const cached = messageCache.value[fromId] || []
  const nextCache = { ...messageCache.value }
  delete nextCache[fromId]
  nextCache[toId] = cached
  messageCache.value = nextCache

  const nextPending = { ...pendingConversations.value }
  delete nextPending[fromId]
  nextPending[toId] = true
  pendingConversations.value = nextPending

  if (activeConversationId.value === fromId) {
    activeConversationId.value = toId
    messages.value = cached
  }
  if (activeRequestConversationId === fromId) {
    activeRequestConversationId = toId
  }
}

async function saveSetup() {
  setupError.value = ''
  if (!apiKey.value.trim()) {
    setupError.value = '请粘贴 API Key'
    return
  }
  try {
    await api('/api/setup', {
      method: 'POST',
      body: JSON.stringify({ provider: provider.value, apiKey: apiKey.value.trim() })
    })
    savedApiKeys.value = { ...savedApiKeys.value, [provider.value]: apiKey.value.trim() }
    apiKey.value = ''
    await boot()
  } catch (error) {
    setupError.value = error instanceof Error ? error.message : '保存失败'
  }
}

function openSettings() {
  settingsProvider.value = provider.value
  settingsApiKey.value = savedApiKeys.value[settingsProvider.value] || ''
  settingsError.value = ''
  settingsOpen.value = true
}

function selectSettingsProvider(nextProvider: Provider) {
  settingsProvider.value = nextProvider
  settingsApiKey.value = savedApiKeys.value[nextProvider] || ''
  settingsError.value = ''
}

function closeSettings() {
  settingsOpen.value = false
  settingsError.value = ''
}

async function saveSettings() {
  settingsError.value = ''
  if (!settingsApiKey.value.trim()) {
    settingsError.value = '请粘贴新的 API Key'
    return
  }
  loading.value = true
  try {
    await api('/api/setup', {
      method: 'POST',
      body: JSON.stringify({ provider: settingsProvider.value, apiKey: settingsApiKey.value.trim() })
    })
    provider.value = settingsProvider.value
    savedApiKeys.value = { ...savedApiKeys.value, [settingsProvider.value]: settingsApiKey.value.trim() }
    closeSettings()
    await boot()
  } catch (error) {
    settingsError.value = error instanceof Error ? error.message : '保存失败'
  } finally {
    loading.value = false
  }
}

async function refreshConversations() {
  const data = await api<{ conversations: Conversation[] }>('/api/conversations')
  conversations.value = data.conversations
}

async function openConversation(id: string) {
  activeConversationId.value = id
  messages.value = messageCache.value[id] || []
  if (!messageCache.value[id] || !pendingConversations.value[id]) {
    const data = await api<{ conversation: Conversation; messages: Message[] }>(`/api/conversations/${id}`)
    activeConversationId.value = data.conversation.id
    updateConversationInList(data.conversation as Conversation)
    if (!pendingConversations.value[id]) {
      setCachedMessages(id, data.messages)
    }
  }
  await scrollToBottom()
}

async function newConversation() {
  const conversation = await api<Conversation>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title: '新会话', model: providerModel.value })
  })
  await refreshConversations()
  activeConversationId.value = conversation.id
  messages.value = []
  setCachedMessages(conversation.id, [])
}

function defaultConversationTitle(title: string) {
  return title.trim() === '' || title.trim() === '新会话'
}

async function maybeRenameConversationFromFirstMessage(conversationId: string, text: string) {
  const conversation = conversations.value.find((item) => item.id === conversationId)
  if (!conversation || !defaultConversationTitle(conversation.title)) return
  await renameConversation(conversationId, text.slice(0, 80))
}

function openTitleDialog() {
  if (!activeConversation.value) return
  titleDraft.value = activeConversation.value.title
  titleError.value = ''
  titleDialogOpen.value = true
}

function closeTitleDialog() {
  titleDialogOpen.value = false
  titleError.value = ''
}

async function saveConversationTitle() {
  const conversationId = activeConversationId.value
  if (!conversationId) return
  const title = titleDraft.value.trim()
  if (!title) {
    titleError.value = '请输入会话名称'
    return
  }
  try {
    await renameConversation(conversationId, title)
    closeTitleDialog()
  } catch (error) {
    titleError.value = error instanceof Error ? error.message : '保存失败'
  }
}

async function renameConversation(conversationId: string, title: string) {
  const normalizedTitle = title.trim()
  if (!normalizedTitle) return
  const updated = await api<Conversation>(`/api/conversations/${conversationId}/title`, {
    method: 'POST',
    body: JSON.stringify({ title: normalizedTitle })
  })
  updateConversationInList(updated)
}

async function startInlineTitleEdit(conversation: Conversation) {
  editingConversationId.value = conversation.id
  editingConversationTitle.value = conversation.title
  await nextTick()
  document.getElementById(`title-editor-${conversation.id}`)?.focus()
}

async function saveInlineTitleEdit() {
  const conversationId = editingConversationId.value
  if (!conversationId) return
  const nextTitle = editingConversationTitle.value.trim()
  const currentTitle = conversations.value.find((item) => item.id === conversationId)?.title || ''
  editingConversationId.value = null
  editingConversationTitle.value = ''
  if (!nextTitle || nextTitle === currentTitle) return
  await renameConversation(conversationId, nextTitle)
}

function cancelInlineTitleEdit() {
  editingConversationId.value = null
  editingConversationTitle.value = ''
}

async function deleteConversationItem(conversationId: string) {
  await api(`/api/conversations/${conversationId}/delete`, { method: 'POST', body: '{}' })
  const nextCache = { ...messageCache.value }
  delete nextCache[conversationId]
  messageCache.value = nextCache
  const nextPending = { ...pendingConversations.value }
  delete nextPending[conversationId]
  pendingConversations.value = nextPending
  conversations.value = conversations.value.filter((item) => item.id !== conversationId)
  if (activeConversationId.value === conversationId) {
    const nextConversation = conversations.value[0]
    if (nextConversation) {
      await openConversation(nextConversation.id)
    } else {
      activeConversationId.value = null
      messages.value = []
    }
  }
}

function setComposerMode(mode: ComposerMode) {
  composerMode.value = mode
}

function fileToUploadImage(file: File): Promise<UploadImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      const commaIndex = result.indexOf(',')
      resolve({
        name: file.name,
        type: file.type || 'image/png',
        data: commaIndex >= 0 ? result.slice(commaIndex + 1) : result,
        preview: result
      })
    }
    reader.onerror = () => reject(new Error('图片读取失败'))
    reader.readAsDataURL(file)
  })
}

async function onImageUpload(event: Event) {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files || []).filter((file) => file.type.startsWith('image/'))
  if (!files.length) return
  const nextImages = await Promise.all(files.map(fileToUploadImage))
  uploadImages.value = [...uploadImages.value, ...nextImages].slice(0, 4)
  target.value = ''
}

function removeUploadImage(index: number) {
  uploadImages.value = uploadImages.value.filter((_, itemIndex) => itemIndex !== index)
}

function clearUploadImages() {
  uploadImages.value = []
  if (fileInput.value) fileInput.value.value = ''
}

async function sendMessage() {
  if (composerMode.value === 'image') {
    await generateImage()
    return
  }
  const text = input.value.trim()
  if (!text || activeConversationPending.value) return
  const pendingUploads = [...uploadImages.value]
  const images = pendingUploads.map(({ name, type, data }) => ({ name, type, data }))
  input.value = ''
  resizeComposer()
  clearUploadImages()
  const controller = new AbortController()
  activeRequestController = controller
  let requestConversationId = activeConversationId.value || `pending_${Date.now()}`
  activeRequestConversationId = requestConversationId
  cancelledRequestIds.delete(requestConversationId)
  activeConversationId.value = requestConversationId
  setConversationPending(requestConversationId, true)
  appendCachedMessages(requestConversationId, [{ role: 'user', content: `${text}${referenceMarkdown(pendingUploads)}` }, { role: 'assistant', content: '' }])
  await scrollToBottom()

  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Device-Token': await ensureDeviceToken()
      },
      signal: controller.signal,
      body: JSON.stringify({ conversationId: requestConversationId.startsWith('pending_') ? null : requestConversationId, message: text, images, model: providerModel.value })
    })
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}))
      throw new Error(data.error || '请求失败')
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finished = false
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split('\n\n')
      buffer = events.pop() || ''
      for (const rawEvent of events) {
        const eventName = rawEvent.match(/^event: (.+)$/m)?.[1]
        const dataLine = rawEvent.match(/^data: (.+)$/m)?.[1]
        if (!eventName || !dataLine) continue
        const data = JSON.parse(dataLine)
        if (isRequestCancelled(requestConversationId)) {
          finished = true
          break
        }
        if (eventName === 'meta') {
          if (requestConversationId !== data.conversationId) {
            if (isRequestCancelled(requestConversationId)) {
              cancelledRequestIds.add(data.conversationId)
            }
            replaceCachedConversationId(requestConversationId, data.conversationId)
            requestConversationId = data.conversationId
          }
          await maybeRenameConversationFromFirstMessage(requestConversationId, text)
          await refreshConversations()
        } else if (eventName === 'delta') {
          if (isRequestCancelled(requestConversationId)) continue
          updateCachedLastAssistant(requestConversationId, (last) => {
            last.content += data.text
          })
          await scrollToBottom()
        } else if (eventName === 'done') {
          finished = true
          break
        } else if (eventName === 'error') {
          throw new Error(data.message)
        }
      }
      if (finished) {
        await reader.cancel().catch(() => undefined)
        break
      }
    }
    await refreshConversations()
  } catch (error) {
    const last = messageCache.value[requestConversationId]?.[messageCache.value[requestConversationId].length - 1]
    if (last?.role === 'assistant') {
      if ((isAbortError(error) || isRequestCancelled(requestConversationId)) && !last.content && !last.imageUrl) {
        removeCachedLastMessage(requestConversationId)
      } else if (!isAbortError(error) && !isRequestCancelled(requestConversationId)) {
        updateCachedLastAssistant(requestConversationId, (message) => {
          message.content = error instanceof Error ? error.message : '请求失败'
        })
      }
    }
  } finally {
    finishActiveRequest(controller, requestConversationId)
  }
}

async function generateImage() {
  const prompt = input.value.trim()
  if (!prompt || !canGenerateImage.value) return
  const pendingUploads = [...uploadImages.value]
  const images = pendingUploads.map(({ name, type, data }) => ({ name, type, data }))
  input.value = ''
  resizeComposer()
  clearUploadImages()
  const controller = new AbortController()
  activeRequestController = controller
  let requestConversationId = activeConversationId.value || `pending_${Date.now()}`
  activeRequestConversationId = requestConversationId
  cancelledRequestIds.delete(requestConversationId)
  activeConversationId.value = requestConversationId
  setConversationPending(requestConversationId, true)
  appendCachedMessages(requestConversationId, [
    { role: 'user', content: `生成图片：${prompt}${referenceMarkdown(pendingUploads)}` },
    { role: 'assistant', content: '图片生成时间较长，请稍等1分钟左右，有问题联系客服～' }
  ])
  await scrollToBottom()
  try {
    const data = await api<{ conversationId: string; imageUrl: string }>('/api/image', {
      method: 'POST',
      signal: controller.signal,
      body: JSON.stringify({ conversationId: requestConversationId.startsWith('pending_') ? null : requestConversationId, prompt, images, quality: 'high' })
    })
    if (isRequestCancelled(requestConversationId)) return
    if (requestConversationId !== data.conversationId) {
      if (isRequestCancelled(requestConversationId)) {
        cancelledRequestIds.add(data.conversationId)
      }
      replaceCachedConversationId(requestConversationId, data.conversationId)
      requestConversationId = data.conversationId
    }
    if (isRequestCancelled(requestConversationId)) return
    updateCachedLastAssistant(requestConversationId, (last) => {
      last.content = '图片已生成'
      last.imageUrl = data.imageUrl
    })
    await refreshConversations()
    await scrollToBottom()
  } catch (error) {
    const last = messageCache.value[requestConversationId]?.[messageCache.value[requestConversationId].length - 1]
    if (last?.role === 'assistant') {
      if (isAbortError(error)) {
        removeCachedLastMessage(requestConversationId)
      } else if (!isRequestCancelled(requestConversationId)) {
        updateCachedLastAssistant(requestConversationId, (message) => {
          message.content = error instanceof Error ? error.message : '生图失败'
        })
      }
    }
  } finally {
    finishActiveRequest(controller, requestConversationId)
  }
}

function onComposerKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendMessage()
  }
}

onMounted(boot)
watch(input, () => {
  resizeComposer()
})
</script>

<template>
  <div class="min-h-screen bg-gray-100 text-gray-950">
    <section v-if="!configured" class="flex min-h-screen items-center justify-center p-6">
      <form class="w-full max-w-md rounded-[28px] bg-white p-8 shadow-panel" @submit.prevent="saveSetup">
        <div class="mb-7 text-center">
          <p class="text-2xl font-semibold tracking-tight">EasyChat</p>
          <p class="mt-2 text-sm text-gray-500">选择服务商并粘贴 API Key</p>
        </div>

        <div class="grid grid-cols-2 gap-2 rounded-2xl bg-gray-100 p-1">
          <button
            type="button"
            class="rounded-xl px-4 py-3 text-sm font-semibold transition"
            :class="provider === 'openai' ? 'bg-white text-primary-700 shadow-card' : 'text-gray-500 hover:text-gray-800'"
            @click="provider = 'openai'"
          >
            OpenAI
          </button>
          <button
            type="button"
            class="rounded-xl px-4 py-3 text-sm font-semibold transition"
            :class="provider === 'claude' ? 'bg-white text-primary-700 shadow-card' : 'text-gray-500 hover:text-gray-800'"
            @click="provider = 'claude'"
          >
            Claude
          </button>
        </div>

        <label class="mt-5 grid gap-2">
          <span class="text-sm font-medium text-gray-700">API Key</span>
          <input v-model="apiKey" class="input font-mono" type="password" placeholder="粘贴你的 API Key" autocomplete="off" />
        </label>

        <button class="btn btn-primary mt-6 h-12 w-full" :disabled="loading">开始聊天</button>
        <p class="mt-3 min-h-5 text-sm text-red-600">{{ setupError }}</p>
      </form>
    </section>

    <main v-else class="grid h-screen grid-cols-[300px_1fr] overflow-hidden">
      <aside class="flex min-h-0 flex-col border-r border-slate-200 bg-slate-950 text-white">
        <div class="border-b border-white/10 p-5">
          <div class="flex items-center justify-between">
            <div>
              <p class="text-lg font-semibold">EasyChat</p>
              <p class="mt-1 text-xs text-slate-400">{{ statusText }}</p>
            </div>
            <span class="rounded-full border border-primary-400/30 bg-primary-400/10 px-2.5 py-1 text-xs text-primary-100">
              {{ providerName(provider) }}
            </span>
          </div>
          <button class="btn mt-5 w-full bg-white text-slate-950 hover:bg-slate-100" @click="newConversation">新会话</button>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto p-3">
          <div
            v-for="conversation in conversations"
            :key="conversation.id"
            class="mb-2 w-full rounded-2xl px-3 py-3 text-left transition"
            :class="conversation.id === activeConversationId ? 'bg-white/15 text-white' : 'text-slate-300 hover:bg-white/10 hover:text-white'"
            @click="editingConversationId === conversation.id ? undefined : openConversation(conversation.id)"
            @dblclick.stop="startInlineTitleEdit(conversation)"
          >
            <div class="group flex items-center gap-2">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <input
                    v-if="editingConversationId === conversation.id"
                    :id="`title-editor-${conversation.id}`"
                    v-model="editingConversationTitle"
                    class="min-w-0 flex-1 rounded-lg border border-white/20 bg-white/10 px-2 py-1 text-sm font-medium text-white outline-none ring-2 ring-primary-400/20"
                    maxlength="80"
                    @click.stop
                    @keydown.enter.prevent="saveInlineTitleEdit"
                    @keydown.esc.prevent="cancelInlineTitleEdit"
                    @blur="saveInlineTitleEdit"
                  />
                  <p v-else class="min-w-0 flex-1 truncate text-sm font-medium">{{ conversation.title }}</p>
                  <span v-if="pendingConversations[conversation.id]" class="h-2 w-2 shrink-0 animate-pulse-soft rounded-full bg-primary-300"></span>
                </div>
                <p class="mt-1 text-xs text-slate-500">{{ formatTime(conversation.createdAt) }}</p>
              </div>
              <button
                class="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-slate-500 opacity-0 transition hover:bg-red-500/10 hover:text-red-200 group-hover:opacity-100"
                title="删除会话"
                @click.stop="deleteConversationItem(conversation.id)"
              >
                <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M3 6h18" />
                  <path d="M8 6V4h8v2" />
                  <path d="M19 6l-1 14H6L5 6" />
                  <path d="M10 11v5" />
                  <path d="M14 11v5" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        <div class="border-t border-white/10 p-3">
          <button class="btn w-full border border-white/10 bg-white/5 text-slate-200 hover:bg-white/10" @click="openSettings">
            更换 API Key
          </button>
        </div>
      </aside>

      <section class="grid min-h-0 grid-rows-[auto_1fr_auto] bg-gray-50">
        <header class="flex items-center justify-between border-b border-gray-200 bg-white px-6 py-4">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <h2 class="truncate text-lg font-semibold">{{ activeConversation?.title || '新会话' }}</h2>
              <button
                v-if="activeConversation"
                class="rounded-lg px-2 py-1 text-xs font-medium text-gray-500 hover:bg-gray-100 hover:text-gray-900"
                @click="openTitleDialog"
              >
                编辑
              </button>
            </div>
            <p class="mt-1 text-sm text-gray-500">{{ providerName(provider) }} · {{ providerModel }}</p>
          </div>
          <span v-if="canGenerateImage" class="rounded-full border border-primary-100 bg-primary-50 px-3 py-1.5 text-xs font-medium text-primary-700">
            {{ models.image }}
          </span>
        </header>

        <div ref="messagesEl" class="min-h-0 overflow-y-auto px-6 py-8">
          <div v-if="!messages.length" class="mx-auto flex h-full max-w-2xl flex-col items-center justify-center text-center">
            <div class="rounded-3xl bg-white p-8 shadow-card">
              <p class="text-2xl font-semibold">今天想聊点什么？</p>
              <p class="mt-3 text-sm leading-6 text-gray-500">新消息会自动创建会话，流式输出会直接出现在这里。</p>
            </div>
          </div>

          <div v-else class="mx-auto flex max-w-4xl flex-col gap-5">
            <article
              v-for="(message, index) in messages"
              :key="message.id || index"
              class="flex"
              :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
            >
              <div
                class="max-w-[78%] rounded-3xl px-5 py-4 shadow-card"
                :class="message.role === 'user' ? 'bg-primary-600 text-white' : 'bg-white text-gray-900'"
              >
                <div
                  class="markdown-body text-sm leading-7"
                  :class="message.role === 'user' ? 'markdown-user' : ''"
                  v-html="renderMarkdown(message.content || (activeConversationPending && index === messages.length - 1 ? ' ' : ''))"
                ></div>
                <div v-if="activeConversationPending && index === messages.length - 1 && !message.content" class="flex items-center gap-2 py-1.5 text-sm text-gray-500">
                  <span class="inline-flex h-6 items-center gap-1 rounded-full bg-gray-100 px-3">
                    <span class="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-primary-500"></span>
                    <span class="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-primary-500 [animation-delay:120ms]"></span>
                    <span class="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-primary-500 [animation-delay:240ms]"></span>
                  </span>
                  <span>AI 思考中</span>
                </div>
                <div v-if="message.imageUrl" class="mt-4">
                  <button class="block overflow-hidden rounded-2xl border border-gray-100 bg-gray-50" @click="openImagePreview(message.imageUrl)">
                    <img class="max-h-[420px] object-contain" :src="message.imageUrl" alt="生成图片" />
                  </button>
                </div>
              </div>
            </article>
          </div>
        </div>

        <footer class="border-t border-gray-200 bg-white p-4">
          <div class="mx-auto max-w-4xl rounded-3xl border border-gray-200 bg-gray-50 p-3">
            <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div class="flex min-w-0 flex-wrap items-center gap-2">
                <div class="grid grid-cols-2 gap-1 rounded-2xl bg-white p-1 shadow-card">
                  <button
                    class="rounded-xl px-4 py-2 text-sm font-semibold transition"
                    :class="composerMode === 'chat' ? 'bg-slate-950 text-white' : 'text-gray-500 hover:text-gray-900'"
                    @click="setComposerMode('chat')"
                  >
                    聊天
                  </button>
                  <button
                    class="rounded-xl px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
                    :class="composerMode === 'image' ? 'bg-primary-600 text-white' : 'text-gray-500 hover:text-gray-900'"
                    :disabled="!canGenerateImage"
                    @click="setComposerMode('image')"
                  >
                    生图
                  </button>
                </div>

                <div v-if="composerMode === 'chat'" class="flex min-w-0 items-center gap-1 rounded-2xl bg-white p-1 shadow-card">
                  <span class="shrink-0 px-2 text-xs font-medium text-gray-400">聊天模型</span>
                  <select
                    class="min-w-0 max-w-[260px] truncate rounded-xl bg-gray-100 px-3 py-2 text-sm font-medium text-gray-800 outline-none transition hover:bg-gray-200 focus:ring-2 focus:ring-primary-500/20 disabled:cursor-not-allowed disabled:opacity-60"
                    :value="providerModel"
                    :disabled="activeConversationPending"
                    @change="selectChatModel"
                  >
                    <option v-for="model in chatModelOptions" :key="model" :value="model">
                      {{ model }} · {{ modelStatusLabel(provider, model) }}
                    </option>
                  </select>
                  <span
                    class="shrink-0 rounded-xl px-2 py-1 text-xs font-medium"
                    :class="{
                      'bg-emerald-50 text-emerald-700': activeModelTest.status === 'ok',
                      'bg-red-50 text-red-700': activeModelTest.status === 'bad',
                      'bg-amber-50 text-amber-700': activeModelTest.status === 'testing',
                      'bg-gray-100 text-gray-500': activeModelTest.status === 'unknown'
                    }"
                    :title="activeModelStatusText()"
                  >
                    {{ modelStatusLabel(provider, providerModel) }}
                  </span>
                  <button
                    class="grid h-9 w-9 shrink-0 place-items-center rounded-xl text-gray-500 transition hover:bg-gray-100 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
                    :class="modelLoading ? 'animate-spin' : ''"
                    :disabled="modelLoading"
                    title="刷新模型"
                    @click="refreshAvailableModels"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
                      <path d="M21 3v6h-6" />
                    </svg>
                  </button>
                </div>
                <div v-else class="flex min-w-0 items-center gap-1 rounded-2xl bg-white p-1 shadow-card">
                  <span class="shrink-0 px-2 text-xs font-medium text-gray-400">生图模型</span>
                  <span class="truncate rounded-xl bg-primary-50 px-3 py-2 text-sm font-medium text-primary-700">{{ models.image }}</span>
                </div>
              </div>
              <button class="btn btn-secondary py-2" @click="fileInput?.click()">上传图片</button>
              <input ref="fileInput" class="hidden" type="file" accept="image/*" multiple @change="onImageUpload" />
            </div>
            <p v-if="modelError" class="mb-3 truncate text-xs text-amber-600" :title="modelError">{{ modelError }}</p>
            <p v-if="composerMode === 'chat' && activeModelTest.status !== 'unknown'" class="mb-3 truncate text-xs text-gray-500" :title="activeModelStatusText()">
              {{ activeModelStatusText() }}
            </p>

            <div v-if="uploadImages.length" class="mb-3 flex gap-2 overflow-x-auto">
              <div v-for="(image, index) in uploadImages" :key="`${image.name}-${index}`" class="relative h-20 w-20 shrink-0 overflow-hidden rounded-2xl border border-gray-200 bg-white">
                <img class="h-full w-full object-cover" :src="image.preview" :alt="image.name" />
                <button class="absolute right-1 top-1 grid h-6 w-6 place-items-center rounded-full bg-black/60 text-xs text-white" @click="removeUploadImage(index)">×</button>
              </div>
            </div>

            <div class="flex items-end gap-3">
              <textarea
                ref="composerTextarea"
                v-model="input"
                class="input min-h-[52px] resize-none border-0 bg-white py-3 shadow-card"
                rows="1"
                :placeholder="inputPlaceholder"
                @input="resizeComposer"
                @keydown="onComposerKeydown"
              ></textarea>
              <button
                v-if="activeConversationPending"
                class="btn h-[52px] w-20 shrink-0 whitespace-nowrap border border-red-200 bg-red-50 px-0 text-red-700 hover:bg-red-100"
                @click="stopCurrentRequest"
              >
                中止
              </button>
              <button v-else class="btn btn-primary h-[52px] w-20 shrink-0 whitespace-nowrap px-0" :disabled="sendDisabled" @click="sendMessage">发送</button>
            </div>
            <p v-if="composerMode === 'image' && !canGenerateImage" class="mt-2 text-xs text-red-500">生图仅支持 OpenAI。</p>
            <p class="mt-2 text-xs text-gray-400">会话保存在当前设备，不支持会话跨端同步。</p>
          </div>
        </footer>
      </section>
    </main>

    <div v-if="previewImageUrl" class="fixed inset-0 z-50 grid place-items-center bg-black/80 p-6" @click="closeImagePreview">
      <div class="max-h-full max-w-5xl" @click.stop>
        <img class="max-h-[82vh] max-w-full rounded-2xl bg-white object-contain" :src="previewImageUrl" alt="图片预览" />
        <div class="mt-4 flex justify-center gap-3">
          <button class="btn bg-white text-slate-950 hover:bg-gray-100" @click="downloadImage(previewImageUrl)">下载</button>
          <button class="btn border border-white/30 bg-white/10 text-white hover:bg-white/20" @click="closeImagePreview">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="settingsOpen" class="fixed inset-0 z-50 grid place-items-center bg-slate-950/60 p-6" @click="closeSettings">
      <form class="w-full max-w-md rounded-3xl bg-white p-6 shadow-panel" @click.stop @submit.prevent="saveSettings">
        <div class="mb-5">
          <p class="text-lg font-semibold">更换 API Key</p>
          <p class="mt-1 text-sm text-gray-500">新的密钥会绑定到当前设备。</p>
        </div>

        <div class="grid grid-cols-2 gap-2 rounded-2xl bg-gray-100 p-1">
          <button
            type="button"
            class="rounded-xl px-4 py-3 text-sm font-semibold transition"
            :class="settingsProvider === 'openai' ? 'bg-white text-primary-700 shadow-card' : 'text-gray-500 hover:text-gray-800'"
            @click="selectSettingsProvider('openai')"
          >
            OpenAI
          </button>
          <button
            type="button"
            class="rounded-xl px-4 py-3 text-sm font-semibold transition"
            :class="settingsProvider === 'claude' ? 'bg-white text-primary-700 shadow-card' : 'text-gray-500 hover:text-gray-800'"
            @click="selectSettingsProvider('claude')"
          >
            Claude
          </button>
        </div>

        <label class="mt-5 grid gap-2">
          <span class="text-sm font-medium text-gray-700">API Key</span>
          <input v-model="settingsApiKey" class="input font-mono" type="password" placeholder="粘贴新的 API Key" autocomplete="off" />
        </label>
        <p class="mt-2 text-xs text-gray-400">
          {{ activeSavedApiKey ? `${providerName(settingsProvider)} 已保存 Key，可直接修改后保存。` : `尚未保存 ${providerName(settingsProvider)} Key。` }}
        </p>

        <p class="mt-3 min-h-5 text-sm text-red-600">{{ settingsError }}</p>
        <div class="mt-4 flex justify-end gap-2">
          <button type="button" class="btn btn-secondary" @click="closeSettings">取消</button>
          <button class="btn btn-primary" :disabled="loading">保存</button>
        </div>
      </form>
    </div>

    <div v-if="titleDialogOpen" class="fixed inset-0 z-50 grid place-items-center bg-slate-950/60 p-6" @click="closeTitleDialog">
      <form class="w-full max-w-md rounded-3xl bg-white p-6 shadow-panel" @click.stop @submit.prevent="saveConversationTitle">
        <div class="mb-5">
          <p class="text-lg font-semibold">编辑会话名称</p>
        </div>

        <label class="grid gap-2">
          <span class="text-sm font-medium text-gray-700">名称</span>
          <input v-model="titleDraft" class="input" maxlength="80" placeholder="输入会话名称" autocomplete="off" />
        </label>

        <p class="mt-3 min-h-5 text-sm text-red-600">{{ titleError }}</p>
        <div class="mt-4 flex justify-end gap-2">
          <button type="button" class="btn btn-secondary" @click="closeTitleDialog">取消</button>
          <button class="btn btn-primary">保存</button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.markdown-user :deep(code) {
  background: rgba(255, 255, 255, 0.18);
  color: white;
}
</style>
