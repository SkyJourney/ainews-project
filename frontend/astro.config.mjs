// @ts-check
import { defineConfig } from 'astro/config';

import node from '@astrojs/node';
import preact from '@astrojs/preact';

import tailwindcss from '@tailwindcss/vite';

import { unified } from '@astrojs/markdown-remark';

// AInews · Obsidian [[wikilink]] → <a> 转换（自建 mini remark 插件）
import { remarkWikiLink } from './src/lib/wiki-link.ts';

// https://astro.build/config
export default defineConfig({
  // M6：SSR 化，Live Content Collections 请求时查 Postgres（04-roadmap §2.7）
  output: 'server',
  adapter: node({ mode: 'standalone' }),

  integrations: [preact()],

  markdown: {
    // Astro 7 起 remarkPlugins/rehypePlugins 顶层配置已弃用，改走 processor: unified({...})
    processor: unified({ remarkPlugins: [remarkWikiLink] }),
    // Astro 内置 gfm + smartypants 保留（GFM 表格 / 任务列表原生支持）
  },

  vite: {
    plugins: [tailwindcss()]
  }
});
