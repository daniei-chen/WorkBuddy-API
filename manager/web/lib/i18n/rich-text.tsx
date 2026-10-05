import {Fragment} from 'react';

/**
 * 极简富文本：把译文里的 **加粗** 渲染成 <b>、`反引号` 渲染成 <code>。
 *
 * 为什么需要：像“国际版能力边界”这类提示语里有加粗强调，但各语言的语序
 * 不同，拆成多个 JSX 片段翻译必然出现语序错误。让译文自己决定强调位置，
 * 组件只负责渲染，翻译文件里也能一眼看出重点在哪。
 */
export function RichText({text, className}: {text: string; className?: string}) {
  const segments = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter((s) => s !== '');
  return (
    <span className={className}>
      {segments.map((segment, index) => {
        if (segment.startsWith('**') && segment.endsWith('**')) {
          return <b key={index}>{segment.slice(2, -2)}</b>;
        }
        if (segment.startsWith('`') && segment.endsWith('`')) {
          return (
            <code key={index} className="font-mono">
              {segment.slice(1, -1)}
            </code>
          );
        }
        return <Fragment key={index}>{segment}</Fragment>;
      })}
    </span>
  );
}
