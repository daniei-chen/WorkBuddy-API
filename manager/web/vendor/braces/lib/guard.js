'use strict';
// WorkBuddy local patch: bound recursive walkers before entering recursion.
// Parent/prev references are not traversed; only the AST child graph is checked.
exports.validate = ast => {
  const pending = [[ast, 0]];
  const seen = new Set();
  let count = 0;
  while (pending.length) {
    const [node, depth] = pending.pop();
    if (!node || typeof node !== 'object') throw new SyntaxError('Invalid braces AST');
    if (depth > 128 || ++count > 65536 || seen.has(node)) {
      throw new SyntaxError('Braces AST exceeds safe depth or node limit');
    }
    seen.add(node);
    if (node.nodes) {
      if (!Array.isArray(node.nodes) || node.nodes.length > 65536) throw new SyntaxError('Invalid braces AST children');
      for (const child of node.nodes) pending.push([child, depth + 1]);
    }
  }
};
